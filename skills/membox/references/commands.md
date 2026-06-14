# Membox — Full Command Reference

Complete CLI surface for membox. SKILL.md covers the two high-frequency
paths (session-start recall, session-end checkpoint); this file is the
exhaustive reference for everything else. Read the relevant section on
demand rather than loading it all.

## Contents

- [Knowledge Graph](#knowledge-graph-spec_01-core)
- [Trace Layer](#trace-layer-lifecycle-phase-b)
- [Memory Units](#memory-units-lifecycle-phase-cd)
- [Workflow Distillation](#workflow-distillation-lifecycle-phase-f)
- [Document Ingestion](#document-ingestion)
- [Remember a Document without an LLM](#remember-a-document-into-the-knowledge-graph-no-llm-needed)
- [Manual Checkpoint (per-step)](#manual-checkpoint-per-step)
- [Periodic Maintenance](#periodic-maintenance)

## Knowledge Graph (spec_01 core)

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

## Trace Layer (lifecycle Phase B)

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

## Memory Units (lifecycle Phase C–D)

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

## Workflow Distillation (lifecycle Phase F)

```bash
# Identify repeated workflows worth packaging (read-only)
membox distill --project myrepo --dry-run
membox distill --project myrepo --since 30d --dry-run     # relative form OK here
membox distill --project myrepo --root /path/to/project --dry-run
```

> `distill --apply` is reserved for a future phase and currently exits with
> "not yet implemented" — use `--dry-run`.

## Document Ingestion

```bash
# Ingest project documentation into the knowledge graph
membox ingest-file docs/spec.md --project myrepo
membox ingest-file docs/architecture.md --project myrepo --sync

# Check ingest queue status
membox queue
membox process   # drain remaining items
```

## Remember a Document into the Knowledge Graph (no LLM needed)

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

## Manual Checkpoint (per-step)

The `membox checkpoint` wrapper (see SKILL.md) runs pull → triage → extract
in one call. The per-step form has the same effect and is useful for
debugging one phase at a time:

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

## Periodic Maintenance

```bash
# Weekly: check for repeated workflows worth packaging
membox distill --project myrepo --since 30d --dry-run

# Check ingest queue health
membox queue

# Review active units and retract noise
membox memory list --project myrepo --status active_unit
```
