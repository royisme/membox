---
name: membox
description: >-
  Durable, project-scoped memory for coding agents via the local `membox`
  CLI — a knowledge graph + RAG + session-history lifecycle, all local
  SQLite, with no daemon and no API key for the core loop. Use this
  whenever cross-session memory would help — at the START of a session to
  recall prior decisions, conventions, and architecture (e.g. "what did we
  decide about X", "remind me how this works", "have we seen this error
  before"); at the END of a session to checkpoint the session history into
  memory; and any time to ingest, query, or "remember" durable project
  knowledge. Reach for it even when the user doesn't say "membox" — any
  request to recall, persist, or look up lasting project context should
  trigger this skill, since the agent's own context window does not survive
  a restart.
---

# Membox — Local Knowledge Graph + RAG Memory Layer for Coding Agents

Membox gives coding agents durable, project-scoped memory that survives
session restarts. It combines a knowledge graph (entities + relations with
provenance) with FTS and a lifecycle pipeline that turns agent session
history into reusable memory units.

**Design constraints**: local SQLite only, no daemons, no hosted services,
no MCP/HTTP servers. Agents interact via the `membox` CLI.

## When you need more than this page

This page is the high-frequency path: recall at session start, capture at
session end, plus the handful of commands you reach for daily. For anything
deeper, read the bundled references on demand (don't load them preemptively):

- **`references/commands.md`** — the full CLI surface: every `history`,
  `memory`, `distill`, `queue`, and `ingest` subcommand, the no-LLM
  `extract-prompt` → `ingest-graph` flow, and the manual per-step checkpoint.
  Read it when a task needs a command not shown below.
- **`references/hooks.md`** — wiring `SessionStart`/`SessionEnd` hooks so the
  harness runs recall and checkpoint deterministically. Read it when setting
  up automation (a one-time task).

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

This skill ships in the repo at `skills/membox/` (the single source of
truth). Make it discoverable by **symlinking** rather than copying — a copy
silently drifts out of date as the repo's skill evolves, whereas a symlink
always reflects the latest version:

```bash
# Claude Code — user-global (available in every project)
mkdir -p ~/.claude/skills && ln -sfn "$PWD/skills/membox" ~/.claude/skills/membox

# Claude Code — project-local (only this repo; .claude/ is gitignored)
mkdir -p .claude/skills && ln -sfn "$PWD/skills/membox" .claude/skills/membox
```

If your loader cannot follow symlinks and you must copy (`cp -r skills/membox
<dest>`), re-copy after every `git pull` to avoid serving stale docs. Other
agents: point the agent's skill/instruction loader at `skills/membox/`, or
paste `SKILL.md` into the system prompt — it is plain Markdown with no runtime
dependency beyond the `membox` CLI.

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

## Session Start — Recall Context

At the start of every session, recall what the agent already knows about
this project before doing new work:

```bash
# Quick memory recall (crystals + active units, budget-partitioned from graph)
membox query "project context, key decisions, conventions" --include-memory --budget 4000

# Or narrow to a specific topic
membox query "database schema decisions" --include-memory --project myrepo --budget 2000
```

If this is the first session with membox, the result will be minimal.
That is expected — membox grows with use.

## Session End — Capture This Session

> **Session End** → `membox checkpoint` (capture this session into memory),
> and/or `membox ingest-file HANDOFF.md` (archive an explicit handoff).
> **Periodic** → `membox memory consolidate` (dream: promote durable
> cross-session knowledge to crystals) and `membox distill` (package repeated
> workflows). Crystals only appear after enough sessions accumulate — a fresh
> project shows none, and that is expected.

At session end (or after major milestones), feed the session trace into the
lifecycle pipeline. The single-command wrapper is the normal flow:

```bash
# Capture this session: pull → triage → extract in one call (no API key needed)
membox checkpoint --adapt claude --project myrepo
membox checkpoint --adapt claude --project myrepo --dry-run   # preview, persists nothing

# Then on a coarser cadence, promote cross-session crystals
membox memory consolidate --project myrepo --apply
```

The per-step form (`history pull` → `memory triage` → `memory extract` →
`memory consolidate`) has the same effect and is useful for debugging one
phase at a time — see `references/commands.md`.

**Automate it.** A skill can only *remind* the model to checkpoint; sessions
often just end before it does. Wire `SessionEnd`/`SessionStart` hooks so the
harness runs capture and recall deterministically — see `references/hooks.md`.

## Everyday Commands

The most common operations beyond recall and checkpoint. The full surface
(history search/fetch, unit management, distillation, the no-LLM ingest flow)
lives in `references/commands.md`.

```bash
# Ingest durable knowledge into the graph (async by default)
membox ingest "auth uses JWT with rotating refresh tokens" --source "auth.md"
membox ingest-file docs/spec.md --project myrepo
membox ingest-file docs/architecture.md --project myrepo --sync   # synchronous

# Query the knowledge graph (graph + FTS fusion, token-budgeted)
membox query "What technologies are used?" --max-hops 2
membox query "key decisions" --include-memory --all-projects

# Inspect memory units
membox memory list --project myrepo --status active_unit
membox memory list --project myrepo --status crystal
membox memory show <unit-id>

# Queue health (ingest is async; drain or inspect it)
membox queue                      # per-status counts + recent failures
membox process                    # drain the ingest queue, exit when empty
```

> **`--since` is ISO-8601 only** on `memory triage`, `memory consolidate`,
> `checkpoint`, and `history search` (e.g. `--since 2026-06-07T00:00:00`).
> Only `membox distill` accepts the relative form (`7d`, `4w`, `12h`). When in
> doubt, omit `--since` — the pipeline processes all unprocessed trace.

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
