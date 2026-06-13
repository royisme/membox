<!-- Design spec for the Claude Code JSONL history importer (read-only, opt-in). -->

# `claude` adapter — read-only Claude Code JSONL importer

**Status**: design accepted (2026-06-12) — pending implementation
**Why now**: co-requisite for manually testing `membox checkpoint` with Claude
Code (the agent under test). Without it, `history pull --adapt` cannot ingest
real Claude sessions. See `docs/issue/checkpoint-command/README.md`.

## Constraint — opt-in, never auto-index

The adapter is invoked **explicitly** (`--adapt claude` with an explicit
`--session-root`); membox must **never** auto-discover/index `~/.claude` by
default. Privacy + prompt-injection risk (the user flagged this re: MiMo's
default-off Claude indexing). Default `session_root` is NOT silently
`~/.claude/projects`; the user passes it (or sets `$MEMBOX_SESSION_ROOT`).

## Contract — implement `HistoryImporter` (`services/importers/base.py:26-57`)

`SourceKind.CLAUDE_JSONL = "claude-jsonl"` is **already declared**
(`model/schema.py:10-21`) — no schema change. New file
`services/importers/claude_jsonl.py`, mirroring `codex_jsonl.py` / `pi_jsonl.py`:

- `format_name = "claude"`.
- `parse(path, *, project=None, offset_bytes=0, next_seq=0, session=None) -> HistoryImportBatch` — pure parser, **no DB, no redaction, no truncation** (full payload in `text`/`body`; store applies `text_cap_bytes`). Deterministic, file-position-independent IDs.
- `discover_sessions(project_cwd, session_root) -> list[Path]` — encode `project_cwd` to Claude's dir name (absolute path with `/` → `-`, e.g. `/Users/royzhu/software/myproject/python/membox` → `-Users-royzhu-software-myproject-python-membox`) and return `sorted(session_root / encoded).glob("*.jsonl")`. Return `[]` if the dir is absent.

Register in `services/importers/__init__.py`: `"claude": ClaudeJsonlImporter`.
Extend `_FORMAT_BY_SOURCE_KIND` in `core/history_import.py:30-35` with
`SourceKind.CLAUDE_JSONL.value: "claude"` so `fetch_payload` resolves it.

Resume/incremental (mtime/offset/next_seq) is owned by the **orchestrator**
(`import_history`); the importer only respects what it's handed. Append-growth
of a live session file resumes from `offset_bytes` cleanly.

## Line handling — Claude JSONL schema (verified on-disk)

Each line is a JSON object dispatched on top-level `type`. Envelope fields
present on chat lines: `uuid`, `parentUuid` (null at root), `sessionId`,
`timestamp` (ISO-8601 `…Z`), `cwd`, `gitBranch`, `version`, `isSidechain`,
`isMeta`.

| `type` | Action |
|---|---|
| `assistant` | → message (`role="assistant"`) + events from `tool_use` blocks |
| `user` | → message (`role="user"`) + events from `tool_result` blocks; skip if `isMeta:true` (synthetic reminders) |
| `system` | → message (`role="system"`), optional; carries error/retry subtypes |
| `ai-title` | session **title** source — capture `aiTitle`, do not emit a message |
| `attachment`, `mode`, `permission-mode`, `last-prompt`, `file-history-snapshot`, `queue-operation` | **skip** (control/UI lines) |

There is **no session-start header**: derive `session.external_id =
sessionId`, `id = f"claude-jsonl:{sessionId}"`, `started_at` = first chat
line's `timestamp`, `ended_at` = last, `title` = last `ai-title` (else first
user text snippet), `project` = `project` arg override else derived from `cwd`,
`source_ref = str(path)`.

### Message mapping (`HistoryMessageRecord`)

- `external_id = uuid`, `parent_id = parentUuid`, `session_id = sessionId`,
  `created_at = timestamp`.
- `role = message.role` (or `"system"`).
- `text`: if `message.content` is a string, use it; if an array, concatenate
  the `text` blocks (skip `tool_use`/`tool_result`/`thinking` — see below).
- `seq = next_seq++` in timestamp order (stable tie-break on parent-chain depth).

**Thinking blocks** (`type:"thinking"` on assistant lines): emit as a
`REASONING` event (kind exists), `body = block.thinking`, not folded into the
message text. (Lets `history search --kind reasoning` work; keep redaction at
the store boundary.)

### Event mapping (`HistoryEventRecord`)

- **`tool_use`** block (assistant) → `kind=TOOL_CALL`, `tool_name = block.name`,
  `anchor = block.id` (`toolu_…`), `message_external_id = line.uuid`,
  `body = json.dumps(block.input)`, `file_path`:
  - Read / Edit / Write → `block.input.file_path`
  - Bash → none in v1 (path is embedded in the `command` string; no reliable
    structured extraction — leave `file_path=None`, body carries the command)
  - others → `None`
- **`tool_result`** block (user) → `kind=TOOL_RESULT`,
  `anchor = block.tool_use_id` (joins to its TOOL_CALL via the `toolu_…` id),
  `is_error = bool(block.is_error)`, `body = stringify(block.content)`,
  `message_external_id = line.uuid`.

`is_error` + `history failures` work out of the box (Bash non-zero exits set
`is_error:true`).

## Subagents / sidechains

Claude Task/Agent subagent transcripts are **separate `.jsonl` files** in the
same project dir, each with its own `sessionId` root; in the parent file the
`Agent` call is a normal `tool_use`. v1: `discover_sessions` returns **all**
`*.jsonl` in the encoded dir, so subagent sessions import as their own
sessions. No cross-file `subagent_id` linking in v1 (matches the deferred
Obelisk subagent/workflow-identity item).

## Non-goals (v1)

- No Bash-command file-path parsing (best-effort later).
- No subagent→parent linkage / workflow grouping.
- No default `~/.claude` indexing; explicit invocation only.

## Implementation checklist

- [ ] `services/importers/claude_jsonl.py`: `ClaudeJsonlImporter` (parse + discover_sessions).
- [ ] Register in `importers/__init__.py`; extend `_FORMAT_BY_SOURCE_KIND`.
- [ ] Skip control line types; `isMeta` user lines; map thinking → REASONING.
- [ ] Path encoding helper (cwd → Claude dir name) with a unit test.
- [ ] Tests: `tests/test_claude_importer.py` — fixture JSONL (assistant+tool_use, user+tool_result with is_error, thinking, ai-title, control lines): assert message/event counts, role/parent threading, tool_name/file_path/is_error, title extraction, incremental resume (offset/next_seq), `history fetch` round-trip via payload_locator.
- [ ] Manual smoke: `membox history pull --adapt claude --session-root ~/.claude/projects` on a real session; `history search/failures/file` return hits.
- [ ] `scripts/update_repository_map.py`; green gate pytest + ruff + mypy.
