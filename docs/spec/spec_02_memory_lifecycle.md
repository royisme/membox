# Membox Spec 02 — Agent Memory Lifecycle (Trace → Unit → Crystal)

Status: Spec — accepted v2.3 (2026-06-11), Phases A–E implemented (Phase E merged 2026-06-12), Phase F merged 2026-06-12 (PR #5)
Date: 2026-06-11
Scope: next-stage memory-system design after graph + FTS retrieval quality gate
Audience: project owner and coding agents reviewing future Membox phases

## Purpose

This document turns the recent external implementation review into an
implementation-facing design that can be reviewed and revised across multiple
agent sessions.

The core recommendation is to evolve Membox from a "KG + RAG retrieval layer"
into a three-stage agent memory system:

```text
Trace -> Unit -> Crystal
```

This is an extension of the current architecture, not a replacement for it.
The existing entity/relation/evidence graph remains the semantic retrieval
index. The new work adds a lifecycle around agent history, triage, durable
memory units, and cross-session consolidation.

## Review Protocol

This spec chapter was accepted on 2026-06-11 and promoted from
`docs/design/agent-memory-lifecycle.md` into `docs/spec/spec_02_memory_lifecycle.md`.
It is now the normative spec chapter for the Agent Memory Lifecycle.

Reviewers should leave feedback in one of these categories:

- **Blocker**: violates a project constraint or would make the implementation
  unsafe.
- **Decision needed**: a product or architecture choice the owner must make.
- **Refine**: wording, naming, schema shape, or phase boundaries need tightening.
- **Implementation note**: useful when turning this design into code issues.

For each revision:

1. Update the "Revision Log" section.
2. Move resolved questions from "Open Questions" into "Decisions".
3. Keep rejected alternatives in the document so future agents do not revisit
   the same path.
4. Do not update `docs/spec.md` until the owner explicitly approves the design.

## Current Constraints

These constraints are inherited from the current project docs and remain
non-negotiable for this design:

- The `membox` CLI is the primary interface.
- Core functionality must work without external services by default.
- Storage stays in local SQLite.
- No hosted vector DB, HTTP server, MCP server, or resident daemon is required
  by default.
- Tests must not depend on external LLMs or embedding APIs.
- Retrieval output must be budgeted and honest about truncation or staleness.
- The current graph + FTS fusion path remains the default semantic query path.

Current implementation reality also matters:

- The shipped retrieval default is graph + FTS fusion.
- The async ingest queue is implemented as a transient worker, not a daemon.
- This design follows the current locked storage decision in `docs/HANDOFF.md`:
  one default SQLite database with `project` columns for scoping. The older
  `docs/spec-v0.2-draft.md` proposal for separate global/project DB files is
  out of scope for this lifecycle track. If the owner reopens that storage
  decision, all schema work in this document must pause until the scope model
  is re-decided.

## Problem Statement

Membox can now retrieve document-backed facts within a token budget. The next
problem is different:

> Coding agents need memory that preserves what happened, decides what is worth
> extracting, and consolidates repeated or durable knowledge without polluting
> the long-term store.

The key challenge is that most conversation content should not become durable
memory, but it should not simply disappear either. "Important to an agent" is
not the same as "important to a human"; "not important now" is not the same as
"not important later".

The system therefore needs explicit lifecycle states instead of a binary
save/discard decision.

## Core Model

### Trace

Trace is raw or lightly normalized agent activity.

Examples:

- Session messages.
- Tool calls and results.
- Tool errors.
- File paths read or edited.
- User instructions.
- Agent/subagent/workflow events.

Trace is optimized for provenance and forensic lookup. It is not injected into
normal query context by default.

### Unit

Unit is an atomic memory candidate extracted from trace or documents.

Examples:

- A project rule explicitly stated by the user.
- A design decision and its rationale.
- A repeated error and its fix.
- A workflow step that may recur.
- A fact extracted from a source document.

Units carry type, scores, temporal metadata, labels, source references, and
status. A unit may be useful but not yet stable.

### Crystal

Crystal is stable, consolidated memory.

Examples:

- A decision confirmed by the user.
- A gotcha observed across multiple sessions.
- A procedure supported by repeated successful use.
- A fact that survived source updates or has high-confidence evidence.

Crystals are eligible for default recall. Units require stricter filters or
explicit inclusion.

## Lifecycle

```text
trace
  -> triaged
  -> unit_candidate
  -> active_unit
  -> crystal_candidate
  -> crystal
  -> archived | superseded | retracted
```

State meanings:

- `trace`: raw activity stored for lookup and provenance.
- `triaged`: cheap gate decision has run.
- `unit_candidate`: extraction produced a structured candidate.
- `active_unit`: saved unit, queryable with explicit filters.
- `crystal_candidate`: enough signal exists to consider consolidation.
- `crystal`: stable memory, eligible for default recall.
- `archived`: retained but excluded from normal retrieval.
- `superseded`: replaced by a newer unit or crystal.
- `retracted`: invalidated by source change or manual correction.

`triaged` is represented by the `history_triage` table, not by
`memory_units.status`. `unit_candidate` and later states are represented by
`memory_units.status`.

### State Transitions

| From | To | Trigger | Notes |
|---|---|---|---|
| `trace` | `triaged` | `membox memory triage --apply` | Writes one `history_triage` row per trace item. |
| `triaged` | `unit_candidate` | `membox memory extract --apply` | Only for triage rows with `should_extract=1`. |
| `unit_candidate` | `active_unit` | Validator pass + activation rule | Active units are searchable only through memory commands, not default `query`. |
| `unit_candidate` | `retracted` | Validator failure after write, manual rejection, or contradiction | Retained for audit; excluded from retrieval. |
| `active_unit` | `crystal_candidate` | Consolidation detects repeated support or explicit user signal | Candidate remains non-default until crystal promotion. |
| `crystal_candidate` | `crystal` | Crystal policy pass | Eligible for `--include-memory` recall. |
| `active_unit` / `crystal` | `superseded` | Newer unit replaces it | `superseded_by` points at replacement. |
| `crystal_candidate` | `active_unit` | Crystal policy rejection | Demotion is audited; the unit stays queryable as an active unit and may re-candidate later. |
| `active_unit` / `crystal` | `archived` | Decay or manual archive | Can be restored. |
| `archived` | prior status | Manual restore or new supporting trace | Restores to the pre-archive status recorded in `memory_unit_status_log`; an archived crystal returns as `crystal`, not as a demoted unit. Restoration is audited. |
| any non-terminal state | `retracted` | Source invalidation or manual correction | Retraction is terminal unless a new unit is created. |

Activation rule for the first implementation:

```text
has_source
AND unit_type is valid
AND labels are valid
AND confidence_score >= 0.50
AND (importance_score >= 0.45 OR user_intent = manual)
```

If a unit does not pass activation, it remains `unit_candidate` only when the
caller requested persistence for review. Otherwise the apply path skips it and
records the reason in the triage/extraction audit output.

## Memory Types

The initial taxonomy should be closed. The model may choose from these values
but must not invent new types at write time:

| Type | Meaning | Typical lifespan | Tie-break priority |
|---|---|---|---|
| `preference` | User or project working preference | Long, unless overridden | 1 |
| `decision` | Choice made with rationale and alternatives | Long | 2 |
| `procedure` | Repeatable workflow or command sequence | Medium to long | 3 |
| `fact` | Objective claim about code, tools, docs, APIs, or project state | Medium; can be superseded | 4 |
| `learning` | Generalized lesson from work or failure | Medium; may become crystal | 5 |
| `plan` | Intended future work | Short | 6 |
| `event` | Something that happened in a session | Short to medium | 7 |
| `context` | Narrow background needed to interpret current work | Short to medium | 8 |

Important distinction:

- `fact`: "PostgreSQL supports JSONB."
- `decision`: "We chose PostgreSQL because it keeps relational constraints and
  JSONB in one local operational store."

Facts need freshness and supersession. Decisions need rationale and provenance.

When a candidate appears to match multiple types, choose the lowest numbered
tie-break priority that fits. For example, "we now run command X before release
because the last build failed" is a `procedure`, not `learning`, because it
prescribes a repeatable future action. `context` is a fallback only for
background that is necessary to interpret other units and does not fit any more
specific type. Context units must have either `valid_to` or an explicit review
horizon; otherwise they should be skipped to avoid unbounded accumulation.

## Triage Gate

The triage gate decides whether trace deserves extraction. It should be cheap,
bounded, and conservative.

Inputs:

- Short trace snippet.
- Local context such as project, session, source path, and neighboring message
  titles.
- Optional user intent signal: manual memory request, explicit "remember this",
  or current command.

Outputs:

```text
should_extract: true | false
unit_type: one of the closed memory types
importance_score: 0.0..1.0
confidence_score: 0.0..1.0
temporal_type: point | range | ongoing | unknown
user_intent: manual | auto
extraction_hint: short phrase for the extractor
reason: short explanation
```

`user_intent` records whether the capture was user-initiated (explicit
"remember this" or a manual memory command) or automatic. The activation rule
reads it, so it persists on the triage row and is copied into the unit's audit
trail.

Design rules:

- Background/automatic triage is strict.
- User-initiated memory capture is lenient but still typed and sourced.
- A rejected trace remains searchable as trace if history indexing is enabled.
- Triage output is not a memory by itself.

The target cost is small enough to run before expensive extraction. In tests,
the default implementation must be deterministic and offline.

### Default Heuristic Gate

The current shipped gate is `heuristic-v4`. It preserves the deterministic v3
triage heuristics and adds the Stabilization Track hardening around the gate:
atomic consolidation apply, FTS-style review pairing, and optional LLM
comparator re-scoring when a caller injects one. `heuristic-v3` remains
available through `--gate heuristic-v3` for one release cycle as an escape
hatch, then should be removed.

LLM-backed triage/comparison can be added behind the same protocol pattern, but
it must not be required for tests or basic use.

The heuristic gate evaluates a bounded text window: the current trace item plus
at most one neighboring user/assistant message on each side, truncated to a
fixed character budget. It returns `should_extract=1` only when one of these
signals fires:

- Explicit memory intent: `remember`, `always`, `never`, `rule`, `decision`,
  `we decided`, `use this going forward`, or Chinese equivalents such as
  `记住`, `以后`, `规则`, `决定`.
- Durable project change: mentions architecture, schema, migration, API
  contract, public CLI behavior, storage, retrieval, or validation gate.
- Repeatable failure/fix: contains an error string plus a resolved/fixed signal.
- Repeatable procedure: contains a command sequence or ordered steps likely to
  recur.
- User correction: explicitly corrects the agent or revises a prior memory.

Baseline scoring:

```text
manual_intent: importance=0.90, confidence=0.85
explicit_decision_or_rule: importance=0.80, confidence=0.75
failure_fix_or_procedure: importance=0.65, confidence=0.65
weak_context_only: importance=0.35, confidence=0.50
```

The heuristic may lower confidence when the text is speculative, contradicted,
or lacks a source path/session reference. The exact keyword lists should live in
code as a small reviewed table, not inside an LLM prompt.

Precision guards:

- Signals are role-weighted. Decision/rule keywords fire at full strength only
  in user messages; the same phrases in assistant output count as weak signals
  and must combine with another signal to pass. "We decided" said by the user
  and by the agent are not the same evidence.
- The durable-project-change signal alone is never sufficient. In this
  repository almost every message mentions architecture, schema, storage, or
  retrieval, so that signal must co-occur with explicit intent, a decision
  phrase, or a failure/fix pattern to set `should_extract=1`.
- Each triage run enforces a per-session extraction cap (configurable, small by
  default) so one noisy session cannot flood the extraction stage.
- `weak_context_only` scores below the activation rule by construction
  (importance 0.35 < 0.45), so the gate sets `should_extract=0` for it by
  default; such rows are extracted only when the caller explicitly requests
  candidates for review. Extracting units that can never activate is pure LLM
  cost.

## Extraction

Extraction turns selected trace into unit candidates.

`membox memory extract` reads unapplied `history_triage` rows where
`should_extract=1`. It should not rescan the whole time window independently;
that would make triage and extraction disagree. `--dry-run` previews extracted
candidates without writing units. `--apply` writes units and marks the triage
rows as consumed.

Extraction granularity: valuable memories (a decision plus its rationale) often
span several adjacent messages. The extractor may cluster pending triage rows
from the same session that fall within a small message-distance window and
extract them as one candidate, recording every contributing trace row in
`memory_unit_sources`. Triage stays per-row; clustering is an extraction-stage
concern.

Idempotency is anchored on source identity, not content. Before writing a unit,
the apply path checks `memory_unit_sources` for an existing non-retracted unit
that already covers the same `(trace_kind, trace_id)`; if one exists, the run
updates or skips instead of inserting. This check spans gate versions: a
re-triage under a newer `gate_version` produces new pending triage rows, but
extraction must still recognize trace that already produced a unit. The
`content_hash` UNIQUE constraint remains as a second-line guard against exact
duplicates, but it cannot be the primary dedup mechanism because LLM-backed
extraction will phrase the same trace differently across runs.

Required fields:

- `title`
- `content`
- `context`
- `unit_type`
- `importance_score`
- `confidence_score`
- `temporal_type`
- `valid_from`
- `valid_to`
- `source_refs`
- `source_message_ids`
- `labels`

Extraction must preserve source references so later recall can use progressive
disclosure:

1. Show a compact unit or crystal.
2. Show source identifiers.
3. Let the agent fetch original trace only when needed.

## Post-Pipeline

The post-pipeline operates after units exist. It should be asynchronous or
manual, not part of the fast capture path.

### Evolve Detection

Detects whether a new unit changes the meaning of an older unit.

Examples:

- A plan becomes a decision.
- A decision becomes a fact about the implemented system.
- A fact is superseded by a newer source version.
- A repeated event becomes a learning.

This is the lifecycle equivalent of relation supersession.

### Entity/KG Extraction

Facts can feed the existing KG index. Decisions remain narrative units by
default; later revisions may add lightweight entity references for decisions
without turning them into KG relations.

Rules:

- The unit remains the source-of-truth memory object.
- The graph remains an index for semantic retrieval.
- Relation evidence points back to units or documents.
- Superseded/retracted units must not appear in default graph retrieval.

### Auto-Label

Auto-labeling may choose only from existing labels or a closed taxonomy.

Do not allow the model to create arbitrary new labels during normal writes.
New label taxonomy is a design decision, not a side effect of extraction.

### Crystal Consolidation

A crystal can be created when one of these conditions holds:

- Explicit user confirmation.
- Multiple independent sources support the same memory.
- The same learning or procedure recurs across sessions.
- A decision is stable and referenced by later work.

Default policy for automatic crystal creation is strict:

```text
explicit_user_confirmation
OR independent_source_count >= 3
OR (unit_type = decision AND confidence_score >= 0.90 AND importance_score >= 0.80)
```

"Independent" is defined as distinct sessions: sources sharing a
`history_sessions.id` (or, for non-trace sources, the same `source_ref`) count
as one. Repeating a statement three times in one conversation is one source.

Score evolution: when consolidation attaches a new independent source to an
existing unit, it may raise `confidence_score` by a small documented increment
(for example +0.05 per independent source, capped at 0.95). This is the only
automatic path by which scores change after extraction. Under the deterministic
heuristic gate the maximum extraction-time confidence is 0.85, so the
`confidence_score >= 0.90` decision branch is reachable only through score
evolution or an LLM-backed gate; this is intentional conservatism, not an
oversight.

The thresholds are calibration targets. They are intentionally conservative for
the first implementation and can be revised only after lifecycle evaluation
exists. `recall_count` / `last_recalled_at` accumulate the usage data that a
future calibration pass will need.

### Decay Execution

No daemon exists, so expiry must be owned by an explicit command:
`memory consolidate --apply` also runs the decay pass. It archives units whose
`valid_to` has passed, and surfaces (in dry-run and apply output) `plan` and
`context` units past their review horizon. Decay only archives — it never
retracts or deletes — and every decay transition is written to
`memory_unit_status_log`. A standalone `membox memory gc` alias may be added if
running decay without consolidation proves useful.

## Proposed Data Model

This is a design sketch, not an approved migration.

All tables below assume the current single-database scope model. `project` is a
normal filter column, not a database boundary.

### Trace Tables

```sql
CREATE TABLE history_sessions (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    started_at      TEXT,
    ended_at        TEXT,
    source_kind     TEXT NOT NULL,
    source_ref      TEXT NOT NULL
);

CREATE TABLE history_messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES history_sessions(id) ON DELETE CASCADE,
    project         TEXT NOT NULL DEFAULT '',
    external_id     TEXT NOT NULL,
    role            TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    parent_id       TEXT,
    text            TEXT NOT NULL DEFAULT '',
    text_truncated  INTEGER NOT NULL DEFAULT 0,
    payload_locator TEXT,
    created_at      TEXT,
    UNIQUE (session_id, external_id)
);

CREATE TABLE history_events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES history_sessions(id) ON DELETE CASCADE,
    project         TEXT NOT NULL DEFAULT '',
    message_id      TEXT REFERENCES history_messages(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    tool_name       TEXT,
    file_path       TEXT,
    body            TEXT NOT NULL DEFAULT '',
    body_truncated  INTEGER NOT NULL DEFAULT 0,
    payload_locator TEXT,
    is_error        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT
);
```

The exact split between messages and events should be validated against real
Codex/Claude/MiMo logs before migration is finalized.

Incremental re-import semantics (required before the migration is finalized):

- Importing the same session file twice, including once mid-session and once
  after the session ended, must converge to the same rows. Append-only growth
  is the easy case: messages are keyed by `(session_id, external_id)`, so new
  lines insert and existing lines upsert in place.
- Upstream rewrites are the hard case. Claude/Codex logs can be compacted,
  sidechained, or merged, which shifts file-order ordinals. Event IDs therefore
  must not depend on file position: `ordinal` means the event's position within
  its parent message (tool call #N of message M), which survives file rewrites,
  not the line number in the JSONL file.
- When an upstream message disappears on re-import (compaction), the importer
  keeps the existing row rather than deleting it; trace is append-only from
  Membox's perspective.
- Importers should track per-source incremental state (path, mtime, lines or
  bytes processed) so repeated imports of large logs stay cheap.

Secret redaction policy (Phase B acceptance requirement):

- Tool outputs routinely contain API keys, tokens, and environment dumps. The
  importer must run a secret-pattern scrubber over `text` / `body` before
  insertion and replace matches with a redaction marker.
- The pattern table lives in code as a small reviewed list (common token
  prefixes, `KEY=`/`TOKEN=` assignments, PEM blocks), like the triage keyword
  table.
- Redaction applies to everything Membox stores (previews and FTS), so secrets
  never become searchable. `history fetch` re-reads the user's own upstream
  file, but the CLI redacts fetch output by default because command output can
  enter an agent context. Raw upstream output requires an explicit `--raw` flag
  and is never persisted or indexed by Membox.
- Redaction is on by default and not silently disablable per import.

ID policy:

- Trace IDs are stable text IDs derived from source data, prefixed by
  `source_kind`, to avoid collisions across Codex, Claude, MiMo, and future
  importers.
- `external_id` preserves the upstream message ID when available.
  Importers must synthesize a stable external ID when the upstream format lacks
  one.
- Events use deterministic IDs based on `(source_kind, session_id, message_id,
  ordinal, kind)` so repeated imports are idempotent. `ordinal` is the event's
  index within its parent message, not a file line number (see incremental
  re-import semantics above).
- Generated integer IDs are reserved for internal memory units.
- `project` is denormalized onto messages and events for filter speed. It must
  match the parent session's project; importers enforce this invariant.

Timestamp policy: `created_at` is nullable because some upstream formats lack
per-message timestamps. Importers must fall back in order: upstream timestamp,
parent message timestamp, session `started_at`. Rows where even the session
start is unknown get NULL and are excluded by `--since` filters; the triage
dry-run reports how many rows were skipped for missing timestamps so the gap is
visible rather than silent.

Valid `source_kind` values for the initial trace layer:

```text
codex-jsonl | claude-jsonl | mimo-sqlite | membox-capture | manual
```

Large payload policy (owner decision, 2026-06-11): Membox does not copy large
payloads at all. Tool outputs already live in the source session log (Codex,
Claude, MiMo all retain them); duplicating them into Membox would bloat the
database for content that is one re-parse away.

- Inline `text` / `body` store a preview capped by `history_text_cap_bytes`;
  the truncated flag marks rows whose preview is partial.
- `payload_locator` records how to find the full payload in the upstream
  source: the session's `source_ref` plus the record's `external_id` (and
  event ordinal where applicable). It is an identity-based locator, not a byte
  offset, so upstream file rewrites do not break it.
- `membox history fetch <id>` re-reads the upstream file on demand and prints
  the full payload with secret redaction applied by default. `--raw` prints the
  fresh upstream content unchanged; fetch output is never persisted or indexed
  by Membox.
- Accepted risk: if the upstream log is deleted or compacted away, the preview
  is all that remains. `history fetch` must say "source no longer available"
  honestly instead of silently returning the preview. Trace previews plus
  units/crystals are designed to survive upstream loss; full re-distillation
  of old payloads is best-effort by construction.
- There is no Membox-managed blob storage in any phase of this track.

### Triage Table

```sql
CREATE TABLE history_triage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project             TEXT NOT NULL DEFAULT '',
    trace_kind          TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    should_extract      INTEGER NOT NULL,
    unit_type           TEXT NOT NULL,
    importance_score    REAL NOT NULL DEFAULT 0,
    confidence_score    REAL NOT NULL DEFAULT 0,
    temporal_type       TEXT NOT NULL DEFAULT 'unknown',
    user_intent         TEXT NOT NULL DEFAULT 'auto',
    extraction_hint     TEXT NOT NULL DEFAULT '',
    reason              TEXT NOT NULL DEFAULT '',
    gate_version        TEXT NOT NULL,
    consumed_at         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (trace_kind, trace_id, gate_version)
);
CREATE INDEX idx_history_triage_pending
    ON history_triage(project, should_extract, consumed_at);
```

`trace_kind` is `message` or `event` in the first implementation. The unique key
lets improved gate versions re-triage old trace without corrupting prior audit
rows. Re-triage under a new gate version creates new pending rows for old
trace; the extraction-stage source-identity check (see Extraction) is what
prevents that from producing duplicate units. When multiple gate versions have
triaged the same trace item, extraction considers only the row from the newest
gate version; older rows stay as audit history.

`trace_id` is a polymorphic reference and cannot be a foreign key. Deleting a
history session cascades through messages and events but leaves triage rows and
unit sources pointing at removed trace. This is accepted: units must survive
trace deletion, and a dangling source is rendered as "source no longer
available (kind/id)" rather than treated as an error. The same rule applies to
`memory_unit_sources.source_ref`.

### Unit Tables

```sql
CREATE TABLE memory_units (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project             TEXT NOT NULL DEFAULT '',
    unit_type           TEXT NOT NULL,
    status              TEXT NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    context             TEXT NOT NULL DEFAULT '',
    importance_score    REAL NOT NULL DEFAULT 0,
    confidence_score    REAL NOT NULL DEFAULT 0,
    temporal_type       TEXT NOT NULL DEFAULT 'unknown',
    valid_from          TEXT,
    valid_to            TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    superseded_by       INTEGER REFERENCES memory_units(id),
    recall_count        INTEGER NOT NULL DEFAULT 0,
    last_recalled_at    TEXT,
    UNIQUE (project, unit_type, content_hash)
);

CREATE TABLE memory_unit_sources (
    unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    source_kind         TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    source_message_id   TEXT NOT NULL DEFAULT '',
    quote               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (unit_id, source_kind, source_ref, source_message_id)
);

CREATE TABLE memory_unit_labels (
    unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    label               TEXT NOT NULL,
    PRIMARY KEY (unit_id, label)
);

CREATE TABLE memory_unit_status_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    from_status         TEXT,
    to_status           TEXT NOT NULL,
    command             TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    source_ref          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);
```

`memory_unit_sources` is the source of truth for provenance. Source shortcuts
such as `source_thread_id` are deliberately not duplicated on `memory_units` in
the first schema. If later profiling proves a denormalized shortcut is needed,
add it with a documented invariant and backfill.

`recall_count` and `last_recalled_at` are reinforcement bookkeeping: Phase E
recall updates them when a unit or crystal is admitted into query output. They
cost nothing to write and are the only data source from which crystal-promotion
thresholds and decay policies can later be calibrated instead of guessed. They
are not used for ranking until a lifecycle eval justifies it.

Deduplication:

- Primary: source identity. The extraction apply path refuses to create a
  second non-retracted unit for trace that already produced one (see
  Extraction). This holds across gate versions and across LLM rephrasings.
- Secondary: `content_hash`, computed over normalized `(unit_type, title,
  content, context, project)`, as a hard guard against exact duplicates from
  manual or document paths.
- A future `membox memory dedup` command can merge legacy duplicates, but the
  first schema should prevent the common rerun duplicate path.

Valid `source_kind` values for `memory_unit_sources`:

```text
history_message | history_event | document | relation | unit | manual
```

`source_ref` holds the corresponding ID or stable source path. `quote` should be
short and source-local; it is evidence preview, not a copy of the full source.

Potential FTS sidecars:

- `history_fts` for trace search.
- `memory_units_fts` for unit/crystal search.

Both should follow the existing CJK-aware FTS direction and avoid raw user MATCH
strings.

## Module Placement and Configuration

New code follows the existing layer boundaries
(`docs/agent/02-architecture-boundaries.md`):

| Code | Location |
|---|---|
| History/triage/unit DDL | `core/store/migrations.py` — next migrations after the current head (check `MIGRATIONS` at implementation time; do not hardcode numbers in this doc) |
| Trace storage ops | `core/store/history.py`, facade methods on `KnowledgeStore` |
| Unit/crystal storage ops | `core/store/memory_units.py`, same facade pattern |
| Heuristic triage gate + keyword/label/secret-pattern tables | `core/triage.py` (pure domain logic, no I/O) |
| Log importers (codex-jsonl, claude-jsonl, …) | `services/importers/` — one module per format behind a shared `HistoryImporter` `Protocol`; file parsing only, no business logic |
| Optional LLM gate/extractor | `services/extraction.py`, behind the existing injectable `Protocol` pattern |
| CLI groups | `cli/commands/history.py`, `cli/commands/memory.py` — presentation only |

Lifecycle states, unit types, labels, and `source_kind` enums are defined once
in `model/schema.py` and imported everywhere else; SQL CHECK constraints are
not used so that enum evolution stays a code change plus migration, not a
table rebuild.

New `MemboxConfig` keys (defaults in parentheses): `history_text_cap_bytes`
(16384), `triage_session_extract_cap` (20), `memory_share` (0.15),
`crystal_confidence_increment` (0.05). All are plain config fields following
the existing per-capability pattern; none require an API key.

## Concurrency Model

The first lifecycle implementation should be single-writer per project for
state-changing lifecycle commands:

- `memory triage --apply`
- `memory extract --apply`
- `memory consolidate --apply`
- `memory supersede`
- `memory retract`
- `memory restore`

Implementation expectation:

- Use the existing SQLite WAL and per-thread connection pattern.
- The store-level `RLock` only protects threads inside one process. Membox's
  real concurrency case is two CLI processes, and the existing answer in this
  codebase is the `worker_lease` row in the `meta` table (see
  `core/worker.py` / `queue.py`). Lifecycle apply commands acquire an
  equivalent per-project lease (`lifecycle_lease:<project>`) before mutating
  state; a second concurrent apply either waits briefly or exits with a clear
  "another apply is running" message. Dry-runs and searches never take the
  lease.
- Within the lease, claiming is still explicit: apply marks triage rows
  consumed (`consumed_at`) inside the same transaction that writes the unit,
  using `BEGIN IMMEDIATE` and status-conditional UPDATEs, so even a lease bug
  cannot produce duplicate units — the source-identity dedup check is the
  final backstop.
- Each apply command should run in one transaction per bounded batch.
- Supersession updates must check that the old row is still in the expected
  status before writing `superseded_by` (`UPDATE ... WHERE status = ?`), and
  treat zero affected rows as "lost the race, skip".

Parallel read/search remains allowed. Parallel dry-runs are allowed because they
do not mutate state.

## Query Behavior

Default `membox query` should continue to use the current graph + FTS fusion.

New explicit modes:

```bash
membox history search "..." --project X
membox memory search "..." --project X --status crystal
membox query "..." --include-memory
membox query "..." --include-history
```

Default recall should prefer crystals over units and units over raw trace.

Recommended output ordering:

1. Graph triples and source chunks from current retrieval.
2. Crystals relevant to the query.
3. Active units, only when requested or when no crystal covers the need.
4. History trace, only when requested or through a follow-up command.

The footer reports every pool that was eligible for this query. History never
enters default output, so the history segment appears only under
`--include-history`; reporting a pool that can never be admitted would only
confuse readers:

```text
(returned 14/40 triples, 4/10 chunks, 2/5 crystals, ~1900/2000 tokens)
(returned 14/40 triples, 4/10 chunks, 2/5 crystals, 3/12 history hits, ~1900/2000 tokens)
```

Memory fusion must not change the default graph + FTS path. `--include-memory`
is opt-in until a lifecycle eval proves it improves agent outcomes without
regressing the existing corpus.

When enabled, memory gets its own budget partition:

```text
memory_reserve = floor(budget * memory_share)
semantic_budget = budget - memory_reserve
```

Initial `memory_share` should be conservative, for example `0.15`, and should
admit crystals before active units (at equal final rank — crystal priority is
a tie-break plus a score boost, not an admission class above relevance; owner
arbitration 2026-06-12, see `docs/plans/plan_05_phase_e_query_fusion.md`
"Owner decisions" #3). If the memory pool has no fitting item, the
unused budget flows back to the existing graph + FTS renderer. Raw history hits
are never admitted into normal `query` output unless `--include-history` is
explicitly set.

Ranking inside the memory pool is not static-score order. Items are ranked by a
combined score of query relevance (FTS rank), stored importance/confidence, and
recency (decayed age since `updated_at` or `last_recalled_at`). Encoding-time
scores alone cannot order recall well: a three-month-old crystal and
yesterday's crystal are not equally useful, and a highly relevant
moderate-score unit should beat an irrelevant high-score one. The exact
weighting is a calibration target for the lifecycle eval; the structure
(relevance x importance x recency) is the design commitment. Admitted items get
their `recall_count` / `last_recalled_at` updated.

Project scoping: trace read commands (`history search`, `history around`,
`history fetch`, `history file`, `history failures`) and `membox memory search`
default to the current project (same resolution as existing commands) and
require an explicit `--all-projects` flag for cross-project output. A single
shared database must not leak another project's session trace by default.

## CLI Surface

Phase-appropriate commands:

```bash
# Trace
membox history pull --adapt pi --project X
membox history pull --adapt codex --project X
membox history pull --adapt membox session.jsonl --project X
membox history search "..." --project X --kind tool_error
membox history around <message-id> --project X
membox history fetch <message-or-event-id> --project X   # redacted by default
membox history fetch <message-or-event-id> --project X --raw
membox history file <path> --project X
membox history failures --project X

# Units
membox memory triage --project X --since 7d --dry-run
membox memory triage --project X --since 7d --apply
membox memory triage --project X --gate heuristic-v3 --apply   # temporary escape hatch
membox memory extract --project X --dry-run
membox memory extract --project X --apply
membox memory extract --project X --gate heuristic-v3 --apply  # temporary escape hatch
membox memory list --project X --status active_unit
membox memory show <id>
membox memory supersede <old-id> <new-id>
membox memory retract <id> --reason "..."
membox memory restore <id>

# Consolidation
membox memory consolidate --project X --since 7d --dry-run
membox memory consolidate --project X --since 7d --apply
membox memory consolidate --project X --since 7d --no-llm --apply

# Workflow packaging, later phase
membox distill --project X --since 30d --dry-run
```

`consolidate` is the primary CLI term. `dream` may be kept as an undocumented or
documented alias only if the owner wants continuity with the external project
terminology.

Command semantics:

- `memory triage --dry-run` previews gate decisions without writing
  `history_triage`.
- `memory triage --apply` writes or updates `history_triage`.
- `memory extract` reads pending `history_triage` rows; it does not rescan the
  raw time window. `--apply` writes units and marks consumed triage rows.
- `memory triage` and `memory extract` default to `heuristic-v4`; `--gate
  heuristic-v3` is a temporary compatibility escape hatch.
- `memory consolidate` reads active units and source trace; it does not run over
  raw trace directly except to verify sources. `--no-llm` explicitly disables
  optional comparator re-scoring; the deterministic path remains the CI default.

## Implementation Phases

### Phase A: Finish Current Retrieval Groundwork

Before adding the lifecycle layer, close the current retrieval branch work:

- Full CJK trigram eval.
- M4 relation supersession.
- M5 close-the-loop ingestion of current project docs.

Reason: lifecycle features should build on stable evidence and supersession
semantics.

### Phase B: History Trace Index

Goal: import and search session history without changing KG behavior.

Deliverables:

- `history_sessions`, `history_messages`, `history_events` tables.
- `history_fts` sidecar.
- Importer contract for `membox-history-jsonl`, a normalized fixture format used
  in tests. `--format` names the file format being parsed; `source_kind` records
  the origin and is carried per record. The fixture format sets
  `source_kind=membox-capture` unless a record specifies its own.
- Secret-redaction scrubber, applied before FTS indexing.
- Per-source incremental import state (path, mtime, progress offset) so
  re-importing a grown log is cheap.
- First real adapter: `codex-jsonl`, unless review finds the local Codex
  history format unavailable or unstable. If so, use `claude-jsonl` as the first
  real adapter and keep `codex-jsonl` queued.
- Search, around, file-history, and failures commands.
- Tests with fixture logs.

Acceptance:

- Import is deterministic and idempotent, including incremental re-import of a
  session file that grew or was rewritten upstream (see incremental re-import
  semantics).
- Secret redaction runs on import; a fixture containing a fake API key must not
  be findable via `history search` or present in stored previews.
- `history fetch` resolves a `payload_locator` back to the upstream file and
  reports honestly when the source is gone. CLI fetch output is redacted by
  default; raw output requires `--raw`.
- Search handles punctuation and CJK safely.
- Filters work by project, session, kind, tool, file path, and time.
- No external LLM or embedding API is required.

### Phase C: Triage and Memory Units

Goal: create typed unit candidates from trace with strict provenance.

Deliverables (the lifecycle eval fixtures are the FIRST deliverable, not a
later gate — the Phase C/D acceptance metrics below cannot be measured without
them, and the heuristic gate's keyword table cannot be tuned blind):

- Lifecycle eval fixture corpus (see Evaluation Strategy) covering at minimum
  triage decisions and extracted unit types.
- `history_triage`, `memory_units`, `memory_unit_sources`,
  `memory_unit_labels`, and `memory_unit_status_log` tables.
- Deterministic heuristic gate for tests.
- Optional LLM-backed gate behind existing provider injection.
- `memory triage` and `memory extract` with explicit `--dry-run` / `--apply`.
- Unit FTS search.
- Offline comparator eval harness:
  `uv run python scripts/eval_lifecycle_comparator.py` replays the committed
  `eval/lifecycle/comparator_cases.yaml` corpus through the v4 comparator path
  and requires agreement with human labels of at least 0.80.

Acceptance:

- Every unit has at least one source.
- Unknown labels and unknown unit types are rejected.
- Dry-run explains create/update/skip decisions.
- Re-running extraction over the same trace is idempotent.
- Apply path is explicit and testable.
- The v4 comparator replay eval passes without external LLM calls; live LLM
  scoring may refresh the captured scores, but CI uses the committed replay
  corpus.

### Phase D: Memory Consolidation

Goal: consolidate units into crystals.

Deliverables:

- Crystal status transition.
- Conflict detection.
- Supersession between units.
- `memory consolidate --dry-run` and `--apply`.
- Validator for source coverage, length, duplicate titles, stale paths, and
  unsupported claims.

Acceptance:

- No source means no crystal.
- Conflicts are surfaced, not silently overwritten.
- User-confirmed units can become crystals with one source.
- Automatic crystals require the approved threshold.

### Phase E: Query Fusion With Memory

Goal: let query optionally include crystals and units.

Deliverables:

- `--include-memory`.
- Budget partition for crystals/units.
- Footer coverage across graph, chunks, crystals, and units.
- Reinforcement metadata if approved.

Acceptance:

- Default query output does not regress current graph + FTS eval.
- Memory inclusion is deterministic in offline tests.
- `--include-memory` uses a calibrated memory budget partition.
- Units do not crowd out source evidence under the default budget.

### Phase F: Distill Workflows

Goal: identify repeated workflows worth packaging.

Deliverables:

- `membox distill --dry-run`.
- Candidate output with evidence, frequency, and recommended form.
- No automatic skill or command creation unless separately approved.

Acceptance:

- A candidate must have repeated evidence or explicit user approval.
- Existing assets are inventoried before proposing a new one.
- The command can return "created nothing" as a successful result.

## Evaluation Strategy

The existing 26-question corpus measures document-backed KG/RAG retrieval. It
does not measure lifecycle quality. The lifecycle eval corpus is built at the
START of Phase C, not before Phase E: the Phase C/D acceptance criteria (triage
precision, type accuracy, duplicate rate, crystal precision) are themselves
measured against these fixtures, so building the corpus last would mean
accepting C and D blind. The single most fragile assumption in this design is
that a cheap triage gate reaches usable precision on real agent trace; the
fixtures are what surface a gate failure early, instead of after months of
noisy accumulation in a real database.

### Lifecycle Eval Corpus

Create fixture sessions with known ground truth:

- Explicit user rules that should become active units.
- Ephemeral chatter that should remain trace only.
- Plans that later become decisions.
- Facts that are superseded by newer sources.
- Repeated failures that should become a learning or procedure.
- Conflicting memories that should be surfaced, not silently merged.
- User corrections that should retract or supersede old units.

Each fixture should include expected outcomes for:

- triage decision
- extracted unit type
- activation status
- consolidation/crystal status
- source references
- expected query inclusion or exclusion

### Metrics

Track at least:

- Triage precision: rejected chatter should not become units.
- Triage recall: explicit user rules/decisions should be selected.
- Type accuracy across the closed taxonomy.
- Duplicate rate after repeated runs.
- Crystal precision: automatically promoted crystals should be supported.
- Query regression: existing graph + FTS eval remains at or above the accepted
  baseline when memory features are disabled.
- Memory-fusion quality: `--include-memory` should add useful context without
  pushing answer-bearing source evidence out of budget.

Phase E cannot be accepted with only unit tests. It needs at least a small
golden lifecycle fixture suite plus the existing retrieval eval.

## Rejected Alternatives

### Store everything as durable memory

Rejected. This creates noise and makes long-term recall worse. Most trace should
remain trace, not become units or crystals.

### Delete the 90 percent that fails triage

Rejected. It may be useless for default recall but still valuable for forensic
lookup. Store it as trace when history indexing is enabled.

### Replace the KG with Markdown memory files

Rejected. Markdown is easy for agents to read but weak for deduplication,
supersession, provenance, and filtering. Membox should keep SQLite as the source
of truth. Markdown export can be added later.

### Auto-run LLM consolidation by default

Rejected for default behavior. It conflicts with CLI-first, no-hidden-work
expectations. Manual or explicitly scheduled consolidation can be considered
later.

### Let the model invent labels and memory types

Rejected. This causes taxonomy drift and weakens downstream filtering. The first
implementation should use closed type and label sets.

### Add a HOT state tier (current task, open loops, session focus)

Rejected for this track. The Sibyl-style tier model includes a working-state
layer; Membox deliberately does not. Working state has opposite mechanics from
long-term memory: tiny capacity, very high churn, no provenance requirement,
and a lifespan of hours. It is better owned by the agent harness and handoff
documents than by a provenance-tracked SQLite lifecycle. Trace, unit, and
crystal map to episodic and consolidated memory; "what am I doing right now" is
not a memory problem. If a future need appears, it should be a separate design,
not a new `memory_units.unit_type`. A placeholder lives in `docs/roadmap.md`
under "Future Tracks" so the idea is not lost. (Owner-confirmed 2026-06-11.)

## Open Questions

All v1 open questions have proposed resolutions in "Decisions" (marked v2,
pending owner sign-off). Remaining open items:

1. The exact per-session extraction cap default and the confidence increment
   for score evolution (+0.05 proposed) — calibrate once the lifecycle eval
   fixtures exist.
2. The exact ranking weights for the Phase E memory pool (relevance x
   importance x recency) — structure is decided, weights are an eval target.

## Decisions

- The current KG/RAG graph remains the semantic retrieval index.
- Trace, unit, and crystal are separate lifecycle stages.
- Raw trace is not injected into default query output.
- Background/automatic capture uses a stricter triage gate than user-initiated
  capture.
- Unit type taxonomy is closed for the initial implementation.
- The lifecycle schema follows the current single SQLite database model with
  `project` columns. Separate global/project DB files are out of scope for this
  track.
- `consolidate` is the primary CLI term; `dream` is at most an alias.
- Triage decisions are persisted in `history_triage`; extraction consumes those
  rows instead of rescanning history.
- The default gate is deterministic and heuristic. LLM-backed triage is optional
  and injected later.
- `unit_candidate` becomes `active_unit` only after source/type/label validation
  and the documented activation rule.
- Automatic crystal promotion starts with a strict threshold:
  user confirmation, at least three independent sources, or a high-confidence
  decision (`confidence_score >= 0.90` and `importance_score >= 0.80`).
- Facts may feed the KG by default. Decisions do not create KG relations by
  default; they may attach entity references later but remain narrative units.
- Memory units are separate from `documents` in the first lifecycle schema to
  avoid destabilizing the current retrieval path.
- Labels are normalized in `memory_unit_labels`, not stored as JSON text.
- Lifecycle writes are single-writer per project; dry-runs and searches can run
  concurrently.
- Reinforcement metadata is deferred until Phase E query fusion.

Owner-confirmed 2026-06-11 (v2.2 discussion):

- No Membox-managed blob storage in any phase. Previews + `payload_locator` +
  `history fetch` re-reading the upstream session log replace the v1/v2 blob
  proposal entirely. (v1 OQ4, superseded by owner direction)
- Markdown export is one-way only. (v1 OQ3)
- The initial closed label set is the 11-label list below, accepted as
  proposed. (v1 OQ2)
- The HOT working-state tier stays out of this track but is recorded in
  `docs/roadmap.md` under "Future Tracks" as a future standalone design.

Owner-confirmed 2026-06-11 (engineering decisions, v2 review pass):

- `dream` is not exposed in public CLI help, not even as a documented alias.
  One verb (`consolidate`) keeps the CLI surface unambiguous; continuity with
  external project terminology is not worth a second name. (v1 OQ1)
- Initial closed label set, deliberately orthogonal to unit types (types say
  what KIND of memory; labels say what AREA it touches): `architecture`,
  `storage`, `retrieval`, `cli`, `testing`, `tooling`, `workflow`,
  `conventions`, `dependencies`, `performance`, `security`. Stored as a
  reviewed constant next to the triage keyword table; changing it is a
  reviewed code change, not a runtime write. (v1 OQ2)
- (Rationale for the one-way Markdown decision above) Re-importing
  human-edited Markdown cannot carry trustworthy provenance and would create a
  second write path competing with the lifecycle; a human who wants to correct
  memory uses `memory supersede` / `memory retract` / manual capture.
- (Owner decision 2026-06-11, supersedes the earlier blob proposal) Membox
  never copies large payloads. Tool outputs already live in the upstream
  session log; Membox stores a capped preview plus a `payload_locator` and
  re-reads the upstream file on demand via `history fetch`. No Membox-managed
  blob storage in any phase. (v1 OQ4)
- Cross-process write coordination uses a per-project lease row in `meta`
  (same pattern as `worker_lease`); the in-process `RLock` is not a
  cross-process mechanism and is not relied on for one.
- Unit deduplication is anchored on source identity (trace already produced a
  unit), with `content_hash` as a secondary exact-match guard.
- Decay is executed by `memory consolidate --apply`; no background process.
- Archived crystals restore as crystals (prior status from the status log),
  not as demoted units.
- "Independent sources" for crystal promotion means distinct sessions.
- Secret redaction at import time is a Phase B acceptance requirement.
- Lifecycle eval fixtures are the first deliverable of Phase C.
- A HOT working-state tier is explicitly out of scope (see Rejected
  Alternatives).
- Trace read commands and `memory search` are project-scoped by default;
  cross-project requires `--all-projects`.

## Review Checklist

For product review:

- Does the design answer "what should be remembered six months from now"?
- Does it separate agent-useful memory from human-useful notes?
- Does it make user intent more important than automatic scoring?
- Does it avoid surprising background work?

For architecture review:

- Does every durable memory have provenance?
- Can stale or contradicted memory be superseded or retracted?
- Is every lifecycle state transition audited?
- Can the feature work offline with fake gates/extractors?
- Does the design preserve the existing retrieval acceptance baseline?
- Are graph, trace, and units kept separate enough to debug ranking failures?

For implementation review:

- Is each phase independently mergeable?
- Does each phase have tests that avoid external LLMs?
- Are FTS query builders safe for punctuation and CJK?
- Are CLI commands explicit and scriptable?
- Does the repository map need updating after file additions?
- Does the lifecycle fixture eval cover triage, extraction, consolidation, and
  query-fusion regressions?

## Revision Log

| Date | Revision | Notes |
|---|---|---|
| 2026-06-11 | v0 review draft | Initial lifecycle design based on Sibyl, Obelisk, MiMo, and Trace/Unit/Crystal review. |
| 2026-06-11 | v1 review response | Addressed first review pass: locked DB scope, specified heuristic triage, added triage table, activation rules, source enums, dedup, status log, label table, concurrency model, memory budget partition, and lifecycle eval strategy. |
| 2026-06-11 | v2.3 accepted | Owner confirmed the nine engineering decisions from the v2 review pass (lease-based cross-process coordination, source-identity dedup, consolidate-owned decay, status-log restore, distinct-session independence, import-time redaction, eval-first Phase C, project-scoped search defaults, no dream alias). Status moved from review draft to accepted; remaining open items are eval-gated calibration parameters only. |
| 2026-06-11 | v2.2 owner decisions | Owner resolved the four product questions: no Membox blob storage in any phase — previews + `payload_locator` + `history fetch` re-read the upstream session log instead (supersedes the v1/v2 blob/truncation proposal); Markdown export one-way; 11-label set accepted; HOT state tier excluded but recorded in roadmap Future Tracks. Renamed `blob_ref` → `payload_locator`; added `history fetch` to the CLI surface and Phase B acceptance; clarified the redaction boundary is what Membox stores, not what the user's own files contain. |
| 2026-06-11 | v2.1 dev cleanup | Pre-commit consistency pass: `user_intent` persisted on triage rows (activation rule reads it), newest-gate-version rule for pending row selection, CLI `--status` value matches state names, `memory restore` added to surface, importer `--format` vs `source_kind` distinction, module placement table, config keys with defaults, migration numbers deliberately not hardcoded. |
| 2026-06-11 | v2 review response | Second review pass (engineering + theory). Blockers fixed: cross-process lease replaces the misattributed RLock claim; dedup re-anchored on source identity across gate versions; incremental re-import semantics and rewrite-safe event ordinals; secret redaction as Phase B acceptance. Decisions resolved: independent-source definition, score evolution (explains the 0.90 branch), archive/restore preserves crystal status, crystal_candidate demotion path, decay owned by consolidate, default project scoping. Theory-driven additions: memory-pool ranking is relevance x importance x recency (not static scores), recall_count/last_recalled_at reinforcement bookkeeping, HOT state tier explicitly rejected, lifecycle eval moved to start of Phase C. Refinements: role-weighted triage signals with per-session cap, weak_context_only not extracted by default, extraction-stage clustering of adjacent triage rows, polymorphic-reference dangling-source policy, timestamp fallback chain, footer omits ineligible pools. All v1 open questions resolved (pending owner sign-off). |
