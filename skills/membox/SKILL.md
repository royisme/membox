---
name: membox
description: Durable, project-scoped memory for coding agents via the local `membox` CLI — a knowledge graph + RAG + session-history lifecycle, all local SQLite, no daemon and no API key for the core loop. Use at the start of a session to recall prior decisions, conventions, and context; at the end of a session to checkpoint the session history into memory; and any time to ingest or query project knowledge.
---

# Membox — Local Knowledge Graph + RAG Memory Layer for Coding Agents

Membox gives coding agents durable, project-scoped memory that survives
session restarts. It combines a knowledge graph (entities + relations with
provenance) with FTS and a lifecycle pipeline that turns agent session
history into reusable memory units.

**Design constraints**: local SQLite only, no daemons, no hosted services,
no MCP/HTTP servers. Agents interact via the `membox` CLI.

## Installation

```bash
# Clone and install (requires Python 3.13+, uv)
git clone <repo-url> membox
cd membox
uv sync

# Verify
uv run membox version
```

The CLI is available as `uv run membox` from the project root, or install
globally with `uv tool install .` (then plain `membox`).

### Installing this skill (so an agent loads it automatically)

This skill ships in the repo at `skills/membox/SKILL.md`. Copy it where your
agent discovers skills:

```bash
# Claude Code — user-global (available in every project)
mkdir -p ~/.claude/skills && cp -r skills/membox ~/.claude/skills/membox

# Claude Code — project-local (only this repo; .claude/ is gitignored)
mkdir -p .claude/skills && cp -r skills/membox .claude/skills/membox
```

Other agents: point the agent's skill/instruction loader at
`skills/membox/SKILL.md`, or paste its contents into the system prompt. The
skill is plain Markdown — no runtime dependency beyond the `membox` CLI.

## Core Concepts

```text
Trace → Unit → Crystal
```

| Layer | What | Commands |
|---|---|---|
| **Trace** | Raw session history (messages, tool events) | `membox history ...` |
| **Unit** | Extracted memory units with status (active, candidate, crystal, superseded) | `membox memory ...` |
| **Crystal** | Promoted durable knowledge worth reusing across sessions | `membox memory consolidate` |
| **Graph** | Entity + relation knowledge graph with provenance | `membox query`, `membox ingest` |
| **Distill** | Repeated workflow detection | `membox distill` |

## Command Reference

### Knowledge Graph (spec_01 core)

```bash
# Ingest text (async by default — enqueues and spawns worker)
membox ingest "codebase-rag is implemented in Python" --source "README.md"

# Ingest a file
membox ingest-file docs/architecture.md
membox ingest-file .handoffs/HANDOFF.md --project myrepo

# Query the knowledge graph (graph + FTS fusion, token-budgeted)
membox query "What technologies are used in the project?" --max-hops 2
membox query "current auth refactor state" --project myrepo --budget 4000
membox query "key decisions" --include-memory         # includes crystal memory
membox query "key decisions" --include-memory --all-projects

# List entities and relations
membox list-entities
membox list-relations --project myrepo

# Async queue management
membox process                    # drain the ingest queue, exit when empty
membox process --retry-failed     # retry failed items (up to 3 attempts)
membox queue                      # show per-status counts and recent failures
```

### Trace Layer (lifecycle Phase B)

```bash
# Pull session history (auto-discovery via MEMBOX_SESSION_ROOT)
export MEMBOX_SESSION_ROOT=~/.claude/projects        # Claude Code
membox history pull --adapt claude --project myrepo

# Adapters (--adapt): claude, codex, pi, membox, membox-capture
export MEMBOX_SESSION_ROOT=~/.pi/agent/sessions      # Pi agent
membox history pull --adapt pi --project myrepo

# Or import a single file directly
membox history pull --adapt membox session.jsonl --project myrepo
membox history pull --adapt codex session.jsonl --project myrepo

# Search history
membox history search "migration error" --project myrepo
membox history search "timeout" --project myrepo --kind tool_error

# Inspect context around a message
membox history around <message-id> --project myrepo

# Fetch original payload from upstream log (redacted by default)
membox history fetch <message-or-event-id> --project myrepo
membox history fetch <message-or-event-id> --project myrepo --raw

# File history and failures
membox history file path/to/file.py --project myrepo
membox history failures --project myrepo
```

### Memory Units (lifecycle Phase C–D)

```bash
# Triage: decide which trace items are worth extracting
membox memory triage --project myrepo --dry-run
membox memory triage --project myrepo --apply

# Extract: create memory units from pending triage rows
membox memory extract --project myrepo --dry-run
membox memory extract --project myrepo --apply

# List and inspect units (status values: unit_candidate, active_unit,
# crystal_candidate, crystal, superseded, archived, retracted)
membox memory list --project myrepo --status active_unit
membox memory list --project myrepo --status crystal
membox memory show <unit-id>

# Consolidate: promote crystals, surface conflicts, run decay
membox memory consolidate --project myrepo --dry-run
membox memory consolidate --project myrepo --apply
```

> **`--since` format gotcha**: on `memory triage`, `memory consolidate`,
> `checkpoint`, and `history search`, `--since` takes an **ISO-8601 timestamp**
> only (e.g. `--since 2026-06-07T00:00:00`). Only `membox distill` accepts the
> relative form (`7d`, `4w`, `12h`). When in doubt, omit `--since` — the
> pipeline processes all unprocessed trace.

```bash
# Manage units
membox memory supersede <old-id> <new-id>
membox memory retract <unit-id> --reason "no longer relevant"
membox memory restore <unit-id>
```

### Workflow Distillation (lifecycle Phase F)

```bash
# Identify repeated workflows worth packaging (read-only)
membox distill --project myrepo --dry-run
membox distill --project myrepo --since 30d --dry-run     # relative form OK here
membox distill --project myrepo --root /path/to/project --dry-run
```

> `distill --apply` is reserved for a future phase and currently exits with
> "not yet implemented" — use `--dry-run`.

## Agent Workflows

### Session Start — Recall Context

At the start of every session, recall what the agent already knows about
this project:

```bash
# Quick memory recall (crystals + active units, budget-partitioned from graph)
membox query "project context, key decisions, conventions" --include-memory --budget 4000

# Or narrow to a specific topic
membox query "database schema decisions" --include-memory --project myrepo --budget 2000
```

If this is the first session with membox, the result will be minimal.
That is expected — membox grows with use.

### Session End — Capture This Session

> **Session End** → `membox checkpoint` (capture this session into memory),
> and/or `membox ingest-file HANDOFF.md` (archive an explicit handoff).
> **Periodic** → `membox memory consolidate` (dream: promote durable
> cross-session knowledge to crystals) and `membox distill` (package repeated
> workflows). Crystals only appear after enough sessions accumulate — a fresh
> project shows none, and that is expected.

At session end (or after major milestones), feed the session trace into
the lifecycle pipeline. The single-command wrapper is the normal flow:

```bash
# Capture this session: pull → triage → extract in one call (no API key needed)
membox checkpoint --adapt claude --project myrepo
membox checkpoint --adapt claude --project myrepo --dry-run   # preview, persists nothing

# Then on a coarser cadence, promote cross-session crystals
membox memory consolidate --project myrepo --apply
```

Manual per-step form (same effect, useful for debugging one phase at a time):

```bash
# 1. Pull the session history
membox history pull --adapt claude --project myrepo

# 2. Triage — decide what is worth keeping
membox memory triage --project myrepo --apply

# 3. Extract — create memory units from triage decisions
membox memory extract --project myrepo --apply

# 4. Consolidate — promote, surface conflicts, decay stale
membox memory consolidate --project myrepo --apply
```

### Periodic Maintenance

```bash
# Weekly: check for repeated workflows worth packaging
membox distill --project myrepo --since 30d --dry-run

# Check ingest queue health
membox queue

# Review active units and retract noise
membox memory list --project myrepo --status active_unit
```

### Document Ingestion

```bash
# Ingest project documentation into the knowledge graph
membox ingest-file docs/spec.md --project myrepo
membox ingest-file docs/architecture.md --project myrepo --sync

# Check ingest queue status
membox queue
membox process   # drain remaining items
```

### Remember a Document into the Knowledge Graph (no LLM needed)

When no LLM is configured (zero-config interactive), the calling agent
**is** the extractor. The two-command flow uses membox only for prompt
construction and storage:

```bash
# 1. Print the canonical extraction prompt (wraps the file + JSON schema)
membox extract-prompt docs/spec.md
membox extract-prompt docs/spec.md --for entities   # --for: entities|query|units

# 2. You run that prompt, produce ExtractedGraph JSON, then feed it back:
membox ingest-graph --from-json - --source docs/spec.md
#   stdin JSON (or --from-json path.json)
#   --source reads the file as the document text; otherwise it is just a label
```

No API key is required — `extract-prompt` is a static template and
`ingest-graph` bypasses the LLM entirely (validates JSON, then stores
entities and relations). Use `membox ingest-file` only when an LLM is
configured and the agent should *not* perform the extraction itself.

## Best Practices

1. **Always use `--project`** to scope operations. Without it, commands
   default to the inferred project from the current directory (its git-root
   name), but explicit scoping prevents cross-project pollution. Note: `pull`
   and `search` must agree on the project name — both infer the git-root name
   when `--project` is omitted.

2. **Dry-run before apply** for triage, extract, and consolidate. Review
   what will change before committing.

3. **Budget queries** with `--budget`. Default is 2000 tokens; increase
   for deeper recall, decrease when context window is tight.

4. **`--include-memory` is additive** — it allocates a separate budget
   partition for memory units alongside graph results, so the graph path
   is never starved.

5. **Run the lifecycle pipeline regularly** (at least after significant
   sessions). A gap of many sessions between runs is fine; the pipeline
   processes all unprocessed trace.

6. **Don't over-ingest** — membox is for durable knowledge, not ephemeral
   chat. Ingest specs, decisions, conventions, and architectural notes.
   Let the trace lifecycle handle session-level memory.

7. **Use `membox distill`** to discover repeated patterns. It is read-only
   and will suggest what workflows might be worth formalizing.

## Environment

| Variable | Purpose | Default |
|---|---|---|
| `MEMBOX_SESSION_ROOT` | Agent session storage root for `history pull` / `checkpoint` auto-discovery | (none) |
| `MEMBOX_INGEST_CONCURRENCY` | Parallel chunk extraction workers | `1` |
| `MEMBOX_DB` | Override the default database path | `~/.membox/membox.db` |

**Database location**: every command accepts `--db PATH` to specify the
database explicitly. The resolution order is: `--db` flag > `MEMBOX_DB` env var
> default `~/.membox/membox.db`. The parent directory is created automatically
on first use. All other configuration is via CLI flags — no config file required.
