<!-- Design spec for `membox checkpoint` — manual one-shot lifecycle capture. -->

# `membox checkpoint` — manual one-shot lifecycle capture

**Status**: design accepted (2026-06-12) — pending implementation
**Owner decisions locked**: top-level command name; default `--apply`.

## Problem

The Trace → Unit → Crystal lifecycle (path B) is the bulk of the engineering,
but the session-**capture** front of it has no natural manual flow. Capturing a
session today requires three ordered commands:

```bash
membox history pull --adapt membox-capture
membox memory triage --apply
membox memory extract --apply
```

The skill (`skills/membox-skill.md`) never documents these steps — it only
drives path A (`ingest-file HANDOFF.md`). So the documented "normal flow"
exercises almost none of the lifecycle. Before real-machine (manual) testing,
the capture front needs a single ergonomic entry point.

This was the chosen remedy (option 2): wrap the capture steps in one command so
the lifecycle has a normal flow that the skill can drive and a human can test
by hand. Automatic (hook-driven, v0.2) triggering comes later and **reuses the
same orchestration function** — see Architecture.

## Responsibility separation — checkpoint / dream / distill

Validated against MiMo-Code's lifecycle, which deliberately splits memory
maintenance into three responsibilities at **different cadences**. Membox
already has two of the three; `checkpoint` adds the capture front:

| Responsibility | Cadence | Membox command |
|---|---|---|
| **checkpoint** — single-session capture/fidelity | every session | **(this doc)** `membox checkpoint` = `pull → triage → extract` |
| **dream** — cross-session durable consolidation & promotion | periodic (coarse) | existing `membox memory consolidate` |
| **distill** — repeated-workflow packaging | periodic (coarser) | existing `membox distill` |

**`checkpoint` does NOT consolidate.** An earlier draft folded
`memory consolidate` into the wrapper; that was wrong on two counts:

1. It conflates per-session fidelity with cross-session integration — the exact
   mix MiMo separates.
2. Crystal promotion requires **≥3 independent sources** (or a high-confidence
   decision). A single session can't meet that bar, so running `consolidate`
   inside a per-session command would almost always promote nothing — pure
   spin. Promotion is inherently cross-session, so it stays a separate command
   on a coarser cadence.

The "0 crystals on first run" experience is therefore expected and correct:
crystals only appear after enough sessions accumulate and `memory consolidate`
runs — not something to paper over by bundling consolidate into capture.

## Non-goals

- Does **not** consolidate / promote to crystal — that is `memory consolidate`
  (the dream responsibility), a separate command on a coarser cadence. No
  `--consolidate` opt-in flag either: keep the responsibilities clean.
- Does **not** recall — `membox query` stays the separate recall path.
- Does **not** ingest documents — `ingest-file HANDOFF.md` (path A) stays
  independent. `checkpoint` only handles the session → lifecycle **capture** side
  of path B.
- No `--handoff FILE` flag in v1 (possible future convenience).

## Dependency — capturing the agent under test

The session/trace layer this command drives is already on par with a
forensic session index (typed `history_events.kind/tool_name/file_path/is_error`,
byte-offset incremental `history_import_state`, `payload_locator` source-of-truth,
`history failures/file/around/search`, episodic/semantic separation via
`history_triage`). The one coverage gap is the **importer**: `IMPORTER_FORMATS`
is `{membox, codex, pi}` — there is **no Claude Code `~/.claude/projects/*.jsonl`
adapter**.

Real-machine testing is done *with* Claude Code, whose sessions live in
`~/.claude/projects`. So a **read-only `claude` adapter** (one more
`HistoryImporter` in `services/importers/`) is a **co-requisite** for manually
testing `checkpoint` against real sessions — otherwise `--adapt` cannot ingest
the agent under test, and the only alternative is a membox-capture hook (which
is the deferred v0.2 automation).

Constraint: the `claude` adapter must be **explicit / opt-in** (invoked with an
explicit `--adapt claude --session-root …`); never auto-index `~/.claude`
by default — privacy and prompt-injection risk. Tracked as sibling work to
this command.

## Architecture

Orchestration lives in the **service layer**, not in the CLI command body.
The current four CLI commands each construct `KnowledgeStore` directly and call
store methods (they do **not** go through `MemoryAgent`); the wrapper composes
the same store calls in one place.

```
core/lifecycle.py
    @dataclass
    class CheckpointResult:
        sessions_pulled: int
        messages_pulled: int
        skipped_lines: int
        triaged_rows: int
        units_created: int
        applied: bool            # False in dry-run

    def run_checkpoint(
        store: KnowledgeStore,
        *,
        project: str,
        session_root: str | None,
        adapt: str,
        since: str | None,
        limit: int,
        apply: bool,
    ) -> CheckpointResult: ...
```

`run_checkpoint` calls, in order (capture only — no consolidation):

1. `core.history_import.history_pull(store, adapt, project=..., session_root=..., text_cap_bytes=...)`
2. `store.trace_rows_for_triage(...)` → `triage_trace(...)` → `store.upsert_history_triage(...)`
3. `store.pending_triage_rows(...)` → `store.get_trace_text(...)` → `store.create_memory_unit(...)` → `store.mark_triage_consumed([...])`

The CLI `checkpoint` command is a **thin shell**: parse args → `run_checkpoint`
→ print summary. The v0.2 hook will call `run_checkpoint` directly, so manual
and automatic capture never diverge.

### Lease

Today triage / extract each acquire+release their own 30 s `lifecycle_lease`;
pull does not lease. After wrapping, `run_checkpoint` acquires **one** lease
around the triage → extract span (pull stays outside the lease, matching
current behavior) and releases it on exit or exception. This prevents the 30 s
TTL expiring mid-chain and serializes concurrent checkpoints.

## CLI surface

Top-level: `membox checkpoint` (registered on the root Typer `app`, delegating
to `run_checkpoint`).

| Option | Default | Meaning |
|---|---|---|
| `--adapt` | `membox-capture` | session transcript format (one of `IMPORTER_FORMATS`) |
| `--project` | inferred from cwd git root | project scope |
| `--session-root` / `$MEMBOX_SESSION_ROOT` | auto-discover | where session transcripts live |
| `--since` | none | ISO-8601 lower bound passed to triage / consolidate |
| `--limit` | 100 | max trace rows for triage/extract (consolidate uses 500) |
| `--apply` / `--dry-run` | **`--apply`** | one flag fans out to all three steps; `--dry-run` previews the whole chain |
| `--db` | `memory.db` | store path |

## Output

Single aggregated summary line on success:

```
✓ checkpoint: pulled 3 sessions (42 msgs) → triaged 18 → extracted 5 units
```

Empty results must read as **expected**, not as a failure (fixes the
"0 units looks like a bug" friction):

- Nothing new pulled: `checkpoint: nothing new to capture since last checkpoint`
- Traces but no units: `checkpoint: captured 18 traces; 0 met the extraction bar (no durable decisions/fixes this session) — this is expected`

`--dry-run` prints the same shape prefixed `would` and applies nothing.

## Idempotency & partial failure

The chain is already safely re-runnable (triage dedupes by gate; extract by
`consumed_at`; `create_memory_unit` returns the existing id for a covered
source). Each step persists independently, so a mid-chain failure leaves a
consistent partial state and **re-running `checkpoint` resumes from the
breakpoint** — no rollback needed. Documented in `--help`.

## Error handling

- Malformed JSONL during pull: keep the existing "report `skipped_lines`,
  exit 1" semantics; surface the count in the summary.
- Lease conflict (concurrent checkpoint): exit 1, `another lifecycle operation
  is in progress`.

## Skill change

Document the three responsibilities at their cadences in
`skills/membox-skill.md`:

> **Session End** → `membox checkpoint` (capture this session into memory),
> and/or `membox ingest-file HANDOFF.md` (archive an explicit handoff).
> **Periodic** → `membox memory consolidate` (dream: promote durable
> cross-session knowledge to crystals) and `membox distill` (package repeated
> workflows). Crystals only appear after enough sessions accumulate — a fresh
> project shows none, and that is expected.

## Follow-ons (tracked, out of v1 scope)

Borrowed from MiMo-Code's lifecycle, but belonging to the **dream**
(`memory consolidate`) stage, not capture:

- **Memory-output validator gate** on promotion: fail/flag entries that bust the
  budget, duplicate an existing crystal, lack why/how, reference unverified
  paths, or carry a vague next-step.
- **MEMORY.md-style structured presentation** of durable knowledge
  (Rules / Architecture decisions / Discovered durable knowledge / Gotchas, each
  with source) — informs crystal / `query --include-memory` output shape, KG
  stays the store of record.
- **FTS query hardening** (phrase-quote, FTS5 special-char safety, BM25 score
  floor, scope/kind/tool/time filters) — orthogonal; verify current state
  separately (relates to S1-D2). Membox's CJK trigram sidecar already beats
  plain unicode61 for Chinese — keep it.

## Implementation checklist

- [ ] `core/lifecycle.py`: `CheckpointResult` + `run_checkpoint` (single lease span).
- [ ] `cli/commands/checkpoint.py`: thin command; register on root `app` in `cli/__init__.py`.
- [ ] Summary + empty-case messaging helper.
- [ ] Tests: `tests/test_checkpoint.py` — full chain on a fixture session
      (asserts counts), empty-session path, dry-run applies nothing, re-run
      idempotency, lease-conflict exit code.
- [ ] Update `skills/membox-skill.md` session-end workflow.
- [ ] `scripts/update_repository_map.py` after adding files.
- [ ] Green gate: pytest + ruff + mypy.
