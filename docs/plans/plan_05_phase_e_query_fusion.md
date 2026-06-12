# Plan 05 — Phase E: Query Fusion With Memory

> **Status**: Draft for owner review — do not execute until accepted. · **Spec source**: `docs/spec/spec_02_memory_lifecycle.md` Phase E (deliverables + acceptance are normative there; this plan only sequences and bounds the work).
> **Predecessor**: Phase D (plan_04) complete — merged to main `1861b8d` (consolidation CLI, crystal policy, gate heuristic-v3, c1–c9 lifecycle fixtures, 535 tests).

## Goal

Let `membox query` optionally include crystals and memory units: `--include-memory` with a calibrated budget partition, footer coverage extended across graph/chunks/crystals/units, and reinforcement bookkeeping (`recall_count`/`last_recalled_at`) on recalled memories — without regressing the default (memory-off) retrieval baseline by a single question.

## Hard regression contract (E0 — holds for every milestone)

- **Default query output is byte-stable when memory features are off.** `--include-memory` defaults to false; no code path may alter default-mode ranking, budgeting, or footer. Gate: `scripts/eval_memory.py --offline --budget 4000` stays 24/26 on every commit, and the Gemini 26/26 baseline is re-verified once before merge (paid run, owner triggers).
- Memory inclusion must be deterministic in offline tests (no LLM in the read path — spec §3.7 unchanged).

## Current state (verify before execution)

- Migration head is **8**. Phase E needs **no schema change**: `memory_units` already has `recall_count`/`last_recalled_at` (reinforcement columns landed with migration 8), and memory-unit FTS sidecars (unicode61 + trigram) exist. If a milestone seems to need DDL, stop and escalate — that contradicts this plan's premise.
- `RetrievalConfig` (src/membox/config.py) carries `budget` (default 2000), `chunk_share`, `fts_fallback_k`. The memory partition slots in beside `chunk_share`.
- The lifecycle fixtures already record per-entry `query_inclusion` expectations: `explicit_memory_only`, `latest_only`, `surface_conflict`, `corrected_only`, `excluded` — written at C1 time precisely so Phase E can assert them. These five behaviors are E4's acceptance matrix.
- Spec open question #2: ranking weights (relevance × importance × recency) — structure is decided, weights are an eval target, owner-calibrated. Treat like the D1 thresholds: named constants, changing them is an owner decision.

## Milestones

### E1 — Memory retrieval pool (store layer, pure read)

- `KnowledgeStore.search_memory_units_for_query(project, query_terms, *, limit)` reusing the existing unit FTS sidecars (unicode61 + trigram CJK path, same pattern as chunk retrieval). Returns active units + crystals only; superseded/retracted/archived are excluded by default (consistent with relation supersession semantics from M4).
- Ranking score: `relevance × importance_score × recency_factor` — relevance from FTS rank (BM25, normalized like the chunk pool), recency a step-down factor on `updated_at` age. Weights as named constants (`MEMORY_RANK_*`) with docstrings marking them owner-calibration targets.
- Crystals rank above non-crystal units at equal score (status tier multiplier — crystal earns its promotion at query time).
- Pure addition: no changes to existing retrieval functions.

### E2 — Budget partition + assembly (`--include-memory`)

- `RetrievalConfig.memory_share` (default ~0.2 of `budget`, named constant, owner-calibration target) — carved from the total budget the same way `chunk_share` is, only when memory is requested. Memory-off → partition identical to today (E0 contract).
- Unit/crystal rendering: one compact line per memory (`[crystal|unit] type: title — content first line`, token-capped), source attribution available but not inline (`memory show <id>` exists for drill-down).
- Spec acceptance "units do not crowd out source evidence": the memory pool can underfill (unused share returns to chunks/graph), but never overfills past `memory_share`.
- CLI: `membox query --include-memory` flag; presentation only, assembly in core.

### E3 — Footer coverage + the five `query_inclusion` behaviors

- Footer extends to `graph n/m · chunks n/m · crystals n/m · units n/m` — honest coverage accounting, same contract as today (no silent truncation).
- Behavior matrix from fixtures: `excluded` (chatter/noise never appears), `explicit_memory_only` (appears only with the flag), `latest_only` (superseded c4 fact never surfaces, its successor does), `corrected_only` (c7 same), `surface_conflict` (both c6 units appear, marked `[conflict]`, neither suppressed).
- Conflict marking is read-side only — consolidation remains the only writer.

### E4 — Reinforcement (the "if approved" spec deliverable — owner decision gate)

- On memory recall in query output: bump `recall_count`, set `last_recalled_at` (write-behind, same transaction discipline as store ops; never blocks the read path on failure).
- **Owner decision before E4 starts**: spec says "reinforcement metadata if approved". Decide: (a) approve as scoped here, (b) defer entirely (E1–E3 ship without it; recency factor then uses `updated_at` only), or (c) approve bookkeeping but exclude it from ranking until calibrated. Recommendation: (c) — write the counters, don't let them feed ranking yet; a feedback loop on ranking is the kind of thing to turn on deliberately with eval coverage, not by default.
- If deferred, the columns stay dormant (they already exist) — zero schema cost.

### E5 — Acceptance: lifecycle query-inclusion harness + eval regression

- Extend `tests/test_lifecycle_acceptance.py`: full pipeline (import → triage → extract → consolidate → query with/without `--include-memory`) asserting every fixture's `query_inclusion` expectation — this is the "small golden lifecycle fixture suite" spec_02 demands beyond unit tests.
- Memory-fusion quality check (spec metric): for a fixture query whose answer lives in source chunks, assert the answer-bearing chunk still appears with memory on (the no-crowding-out acceptance, as a test not a claim).
- Eval regression on every milestone (E0 contract); final pre-merge: offline 24/26 + owner-triggered Gemini 26/26 re-verification.

## Deferred items folded in or explicitly NOT in scope

In scope opportunistically (small, adjacent): consolidate CLI N+1 source-count query (batch it when touching the store layer in E1).
Out of scope (recorded in HANDOFF, unchanged): atomic apply batching, FTS-based conflict candidate pairing, LLM-backed conflict comparator, gate v4 (--help-dump event family), `membox distill` (Phase F).

## Acceptance criteria (spec_02 Phase E, verbatim)

- Default query output does not regress current graph + FTS eval.
- Memory inclusion is deterministic in offline tests.
- `--include-memory` uses a calibrated memory budget partition.
- Units do not crowd out source evidence under the default budget.

## Constraints for all dispatched subagents

- All Phase E work on `feature/phase-e-query-fusion` (single branch, milestone-ordered commits — the plan_04 branch-per-milestone scheme was struck as overhead). Never commit to main/develop.
- No schema changes (head stays 8); if DDL seems needed, escalate.
- No LLM in the read path, ever (spec §3.7).
- Ranking weights and `memory_share` are named constants = owner calibration targets, not tuning knobs.
- Gates per milestone: `uv run pytest` + ruff + strict mypy green; coverage ≥ 80%; offline eval 24/26 @ 4000 on every milestone commit.
- Subagents stop and escalate on judgment calls beyond the brief (notably the E4 reinforcement decision and any ranking-weight change).

## Open questions for the owner (decide at acceptance, not mid-flight)

1. **E4 reinforcement**: approve / defer / bookkeeping-only (recommendation: bookkeeping-only, see E4).
2. **`memory_share` default**: 0.2 proposed (400 tokens of a 2000 default budget; 800 of the 4000 eval budget). Calibration method: fixture-driven — largest share at which E5's no-crowding-out test still passes with margin.
3. **Crystal tier multiplier**: should crystals strictly dominate units in the memory pool (lexicographic) or just get a score boost (proposed: boost, ×1.5, so a highly relevant unit can still beat an off-topic crystal)?
