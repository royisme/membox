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
globally with `uv tool install .`.

## Core Concepts

```
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
membox ingest-file docs/architecture.md --db memory.db
membox ingest-file docs/HANDOFF.md --project myrepo

# Query the knowledge graph (graph + FTS fusion, token-budgeted)
membox query "What technologies are used in the project?" --max-hops 2
membox query "current auth refactor state" --project myrepo --budget 4000
membox query "key decisions" --include-memory         # includes crystal memory
membox query "key decisions" --include-memory --all-projects

# List entities and relations
membox list-entities --db memory.db
membox list-relations --db memory.db --project myrepo

# Async queue management
membox process                    # drain the ingest queue, exit when empty
membox process --retry-failed     # retry failed items (up to 3 attempts)
membox queue                      # show per-status counts and recent failures
```

### Trace Layer (lifecycle Phase B)

```bash
# Pull session history (auto-discovery via MEMBOX_SESSION_ROOT)
export MEMBOX_SESSION_ROOT=~/.pi/agent/sessions   # Pi agent
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
membox memory triage --project myrepo --since 7d --dry-run
membox memory triage --project myrepo --since 7d --apply

# Extract: create memory units from pending triage rows
membox memory extract --project myrepo --dry-run
membox memory extract --project myrepo --apply

# List and inspect units
membox memory list --project myrepo --status active_unit
membox memory list --project myrepo --status crystal
membox memory show <unit-id>

# Consolidate: promote crystals, surface conflicts, run decay
membox memory consolidate --project myrepo --since 7d --dry-run
membox memory consolidate --project myrepo --since 7d --apply

# Manage units
membox memory supersede <old-id> <new-id>
membox memory retract <unit-id> --reason "no longer relevant"
membox memory restore <unit-id>
```

### Workflow Distillation (lifecycle Phase F)

```bash
# Identify repeated workflows worth packaging (read-only)
membox distill --project myrepo --dry-run
membox distill --project myrepo --since 30d --dry-run
membox distill --project myrepo --root /path/to/project --dry-run
```

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

### Session End — Ingest Handoff

At session end (or after major milestones), feed the session trace into
the lifecycle pipeline:

```bash
# 1. Pull the session history
membox history pull --adapt pi

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

## Best Practices

1. **Always use `--project`** to scope operations. Without it, commands
   default to the inferred project from the current directory, but explicit
   scoping prevents cross-project pollution.

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
| `MEMBOX_INGEST_CONCURRENCY` | Parallel chunk extraction workers | `1` |

All other configuration is via CLI flags. No config file is required;
`membox` works with zero configuration for basic usage.
