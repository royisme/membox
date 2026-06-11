# Plan 01 — Phase C: Triage + Memory Units (migration 8)

> **Status**: Ready for execution · **Normative spec**: `docs/spec/spec_02_memory_lifecycle.md` (Phase C section + Triage Gate, Memory Types, Extraction, Proposed Data Model, Concurrency Model, CLI Surface)
> This is a work plan, not a spec. If this document and `spec_02` disagree, `spec_02` wins.

## Goal

Create typed memory-unit candidates from history trace with strict provenance: heuristic triage gate, `memory_units` tables (migration 8), `membox memory triage/extract` with explicit `--dry-run`/`--apply`, per-project `lifecycle_lease`. Phase C produces `unit_candidate` and `active_unit` rows only — crystals, consolidation, decay are Phase D (do not implement them here).

## Hard ordering constraint

**The lifecycle eval fixture corpus is the FIRST deliverable** (owner decision, spec_02). The heuristic gate's keyword table and thresholds cannot be tuned blind; C2+ acceptance is measured against the fixtures. Do not start migration or gate code before C1 is merged.

## Current state (do not redo)

- Migration head is **7** (`core/store/migrations.py` — verify `MIGRATIONS` list at implementation time; Phase C adds entry 8). Pattern: `_DDL_NNNN` string constant for pure DDL, `_migrate_NNNN(conn)` callable when Python logic is needed; each migration runs in its own `BEGIN IMMEDIATE` + `user_version` bump.
- `core/triage.py` exists from Phase B but contains **only** the secret-redaction scrubber (`redact_secrets`, `REDACTION_MARKER`). The gate, keyword tables, and all lifecycle constants are added here.
- History trace tables (`history_sessions/messages/events` + 4 FTS sidecars) and the `membox history` CLI group are live (migration 6).
- `worker_lease` pattern lives in `core/worker.py` / the `meta` table — `lifecycle_lease:<project>` reuses this mechanism, NOT the in-process RLock.
- 492 tests, ~93% coverage, ruff/mypy/pre-commit green on main.

## Milestones

### C1 — Lifecycle eval fixtures (first deliverable)

New `eval/lifecycle/` fixture corpus: synthetic session transcripts (membox-history-jsonl format, so they import via the existing fixture importer) + a `gold.yaml`-style expectations file. **These fixtures are committed** (synthetic content, unlike the gitignored `eval/corpus/`).

Seven required scenario categories (spec_02 Evaluation Strategy):

1. Explicit user rules that should become active units
2. Ephemeral chatter that should remain trace only
3. Plans that later become decisions
4. Facts superseded by newer sources
5. Repeated failures that should become a `learning` or `procedure`
6. Conflicting memories that must be surfaced, not silently merged
7. User corrections that retract/supersede old units

Each fixture entry must declare expected outcomes for: triage decision, extracted unit type, activation status, consolidation/crystal status (recorded now, asserted in Phase D), source references, expected query inclusion/exclusion. Include CJK cases (corpus precedent: ~31% Chinese). Categories 3/4/6/7 need multi-session fixtures (the "independent sources = distinct sessions" rule).

Deliverable includes a loader/assertion harness in `tests/` (offline, deterministic) that later milestones run against. Gate: pytest + ruff + mypy green.

### C2 — Migration 8 + storage ops

Tables per spec_02 Proposed Data Model (DDL is normative there):

- `history_triage` — polymorphic `(trace_kind, trace_id)` ref (NOT a FK), `UNIQUE (trace_kind, trace_id, gate_version)`, pending-index on `(project, should_extract, consumed_at)`.
- `memory_units` — incl. `content_hash` (normalized over `(unit_type, title, content, context, project)`), `superseded_by` self-FK, `recall_count`/`last_recalled_at`, `UNIQUE (project, unit_type, content_hash)`. Status enum: `unit_candidate | active_unit | crystal_candidate | crystal | superseded | archived | retracted`.
- `memory_unit_sources` — PK `(unit_id, source_kind, source_ref, source_message_id)`; `source_kind ∈ history_message | history_event | document | relation | unit | manual`.
- `memory_unit_labels`, `memory_unit_status_log`.
- `memory_units_fts` — CJK-aware, same unicode61 + trigram sidecar pattern as `documents_fts`; never pass raw user MATCH strings.

Storage ops in new `core/store/memory_units.py` on the `KnowledgeStore` facade (same pattern as `history.py`): triage row upsert (gate-version aware), unit CRUD, source/label attach, status transition with `memory_unit_status_log` write, source-identity lookup (`find unit covering (trace_kind, trace_id)` across gate versions, excluding retracted), FTS search. Supersession guard: `UPDATE ... WHERE status = ?`, zero rows affected = lost race, skip.

### C3 — Heuristic gate in `core/triage.py`

Pure domain logic, no I/O. Adds:

- Reviewed constants: 8 unit types with tie-break priority (`preference > decision > procedure > fact > learning > plan > event > context`; `context` is fallback-only and requires `valid_to`/review horizon), the closed 11-label set (`architecture storage retrieval cli testing tooling workflow conventions dependencies performance security`), `source_kind` enum, status enum.
- `GateDecision`-shaped output: `should_extract, unit_type, importance_score, confidence_score, temporal_type (point|range|ongoing|unknown), user_intent (manual|auto), extraction_hint, reason` + `gate_version` string.
- Firing signals over a bounded window (trace item ± 1 neighbor message, fixed char budget): explicit memory intent (`remember/always/never/rule/decision/we decided/记住/以后/规则/决定`), durable project change, error+fixed pair, command-sequence/ordered-steps, user correction.
- Baseline scores: manual_intent 0.90/0.85 · explicit_decision_or_rule 0.80/0.75 · failure_fix_or_procedure 0.65/0.65 · weak_context_only 0.35/0.50. Heuristic confidence caps at 0.85.
- Activation rule: `has_source AND valid type AND valid labels AND confidence >= 0.50 AND (importance >= 0.45 OR user_intent = manual)`.

Keyword tables are tuned against C1 fixtures — record precision/recall numbers in the PR description. Optional LLM-backed gate goes in `services/extraction.py` behind the injectable Protocol (can be a stub/follow-up; the deterministic offline gate is the Phase C requirement).

### C4 — Extraction pipeline + CLI + lease

- `membox memory triage --project X --since 7d --dry-run|--apply` — dry-run previews gate decisions; apply writes `history_triage` rows.
- `membox memory extract --project X --dry-run|--apply` — reads pending `should_extract=1` rows of the **newest gate version only**; may cluster same-session rows within a small message-distance window into one candidate (every contributing row gets a `memory_unit_sources` entry); apply writes units AND sets `consumed_at` in the same transaction.
- **Idempotency anchored on source identity, not content**: before insert, check `memory_unit_sources` for a non-retracted unit covering the same `(trace_kind, trace_id)` → update or skip, never duplicate. `content_hash` UNIQUE is the secondary backstop only.
- Unit management: `membox memory list/show/supersede/retract/restore` (status transitions logged to `memory_unit_status_log`; restore recovers prior status from the log).
- `lifecycle_lease:<project>`: acquired by every mutating apply command (`triage --apply`, `extract --apply`, `supersede`, `retract`, `restore`); same meta-table row protocol as `worker_lease`; concurrent apply waits briefly or exits with "another apply is running". Dry-runs and searches never take the lease.
- CLI modules under `cli/commands/memory.py`, presentation only.

### C5 — Acceptance run

Run the C1 fixture harness end-to-end and record: triage precision (rejected chatter ≠ units), triage recall (explicit rules selected), type accuracy, duplicate rate after repeated runs, and **query regression** (existing graph+FTS eval stays ≥ accepted baseline with memory features disabled — `scripts/eval_memory.py --offline` plus, at milestone close, one full Gemini run).

## Acceptance criteria (spec_02, verbatim)

- Every unit has at least one source.
- Unknown labels and unknown unit types are rejected.
- Dry-run explains create/update/skip decisions.
- Re-running extraction over the same trace is idempotent.
- Apply path is explicit and testable.

## Constraints for all dispatched subagents

- Branch per milestone: `feature/phase-c-fixtures`, `feature/phase-c-migration-8`, `feature/phase-c-gate`, `feature/phase-c-extract-cli`. Never commit to main/develop.
- **Pin migration number 8 in the brief and verify the worktree base is current main** (session-10 lesson: stale worktree base produced wrong migration numbering).
- Do not chain merge + pytest in one backgrounded Bash call (session-8 lesson).
- Gates per milestone: `uv run pytest` + `uv run ruff check` + `uv run mypy src` green; coverage ≥ 80%; Google docstrings; `from __future__ import annotations`; run `scripts/update_repository_map.py` after structural changes.
- Tests use the fake extractor/embedder; mock only at I/O boundaries. SQLite rules: FK ON, WAL, per-thread connections; lifecycle writes use the lease, not the RLock.
- Subagents must stop and escalate on any judgment call beyond the brief (spec ambiguity, schema conflicts, surprising findings) instead of guessing.

## Explicitly out of scope (Phase D)

Crystal transitions and promotion logic (`independent_source_count >= 3`, `decision AND confidence >= 0.90`), conflict detection, consolidation-driven supersession, `memory consolidate`, the validator, score evolution (+0.05/source), decay execution.
