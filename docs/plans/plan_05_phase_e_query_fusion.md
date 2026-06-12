# Plan 05 — Phase E: Query Fusion With Memory

> **Status**: Draft for owner review — do not execute until accepted. · **Spec source**: `docs/spec/spec_02_memory_lifecycle.md` Phase E (deliverables + acceptance are normative there; this plan only sequences and bounds the work).
> **Predecessor**: Phase D (plan_04) complete — merged to main `1861b8d` (consolidation CLI, crystal policy, gate heuristic-v3, c1–c9 lifecycle fixtures, 535 tests).

## Goal

Let `membox query` optionally include crystals and memory units: `--include-memory` with a calibrated budget partition, footer coverage extended across graph/chunks/crystals/units, and reinforcement bookkeeping (`recall_count`/`last_recalled_at`) on recalled memories — without regressing the default (memory-off) retrieval baseline by a single question.

## Hard regression contract (E0 — holds for every milestone)

- **Default query output is byte-stable when memory features are off.** `--include-memory` defaults to false; no code path may alter default-mode ranking, budgeting, or footer. Gate: offline eval stays **exactly 24/26** on every milestone commit. ⚠️ `--check-gates` does NOT enforce this: in `--offline` mode the script skips the hit-rate gate entirely (see the `offline:` docstring in `scripts/eval_memory.py`), and its threshold is ≥0.80 anyway, not exact-count. First task of E1 is therefore a strict wrapper — either `--expect-hits N` in the eval script (exit nonzero unless hit count == N, offline included) or a thin CI script asserting `Overall hit rate: 24/26` in the output; subagent picks the former unless it bloats the script. Every milestone gate below means this strict form. The Gemini 26/26 baseline is re-verified once before merge (paid run — owner-approved, agent-triggered when the API key is available).
- Memory inclusion must be deterministic in offline tests (no LLM in the read path — spec §3.7 unchanged).

## Current state (verify before execution)

- Migration head is **8**. Phase E needs **no schema change**: `memory_units` already has `recall_count`/`last_recalled_at` (reinforcement columns landed with migration 8), and memory-unit FTS sidecars (unicode61 + trigram) exist. If a milestone seems to need DDL, stop and escalate — that contradicts this plan's premise.
- `RetrievalConfig` (src/membox/config.py) carries `budget` (default 2000), `chunk_share`, `fts_fallback_k`. The memory partition slots in beside `chunk_share`.
- The lifecycle fixtures already record per-entry `query_inclusion` expectations: `explicit_memory_only`, `latest_only`, `surface_conflict`, `corrected_only`, `excluded` — written at C1 time precisely so Phase E can assert them. These five behaviors are implemented in E3 and asserted as E5's acceptance matrix.
- Spec open question #2: ranking weights (relevance × importance × recency) — structure is decided, weights are an eval target, owner-calibrated. Treat like the D1 thresholds: named constants, changing them is an owner decision.

## Milestones

### E1 — Memory retrieval pool (store layer, pure read)

- **First deliverable: the strict eval wrapper from E0** (`--expect-hits N` or equivalent), so every subsequent milestone commit has an enforcing gate, not a printing one.
- `KnowledgeStore.search_memory_units_for_query(project, query_terms, *, limit)` reusing the existing unit FTS sidecars (unicode61 + trigram CJK path, same pattern as chunk retrieval). Returns active units + crystals only; superseded/retracted/archived are excluded by default (consistent with relation supersession semantics from M4).
- Ranking score: `relevance × stored_score × recency_factor`, where `stored_score` combines `importance_score` and `confidence_score` (spec_02 §query-fusion names all three components: query relevance, stored importance/confidence, recency). Proposed first cut: `stored_score = importance_score × confidence_score` (both already 0–1; a high-importance low-confidence unit should rank below an equally important confirmed one — confidence is exactly what Phase D score evolution maintains). Relevance from FTS rank (BM25, normalized like the chunk pool); recency a step-down factor on age since `updated_at` (spec names `last_recalled_at` as an alternative anchor, but owner decision #1 keeps recall counters out of ranking for now — switching the anchor to `COALESCE(last_recalled_at, updated_at)` is part of the future enable-reinforcement decision). Weights as named constants (`MEMORY_RANK_*`) with docstrings marking them owner-calibration targets.
- Crystal tier: `MEMORY_CRYSTAL_BOOST = 1.5` multiplier in the final score, plus crystal-first tie-break at equal score — per owner decision #3 (spec line-825 arbitration; see Owner decisions). Relevance can still beat an off-topic crystal.
- Pure addition: no changes to existing retrieval functions.

### E2 — Budget partition + assembly (`--include-memory`)

- `RetrievalConfig.memory_share` (default **0.15** per spec_02's `MemboxConfig` definition — owner decision #2 below; 0.2 is a calibration candidate, not the start). **Exact three-pool budget math, in order**: (1) memory off → the entire pipeline runs untouched on the full `budget` (E0 byte-stability); (2) memory on → `memory_budget = floor(budget × memory_share)`, the existing graph/chunk fusion (including its internal `chunk_share` split and leftover-rollback behavior, unchanged) runs on `budget − memory_budget`; (3) the memory pool fills under `memory_budget`; (4) any unused memory budget is returned to the graph+chunk renderer as extra budget (spec: "unused budget flows back to the existing graph + FTS renderer") — implemented by assembling the memory pool *first* so the actual remainder is known before the fusion pass runs. Memory may underfill, never overfill past `memory_budget`.
- Unit/crystal rendering: one compact line per memory (`[crystal|unit] type: title — content first line`, token-capped), source attribution available but not inline (`memory show <id>` exists for drill-down).
- **Project scoping (spec hard requirement)**: the memory pool defaults to the current project (same `_default_project` resolution as `memory search`/`history` commands) and requires an explicit `--all-projects` for cross-project memory in query output — a shared DB must not leak another project's memories by default. Note `membox query` currently allows `project=None` for graph/chunk retrieval; that existing behavior stays untouched (E0), the scoping rule binds the *memory pool only*. If `--include-memory` is passed and no project can be resolved, error out rather than silently going cross-project.
- CLI: `membox query --include-memory` (+ `--all-projects` interaction above); presentation only, assembly in core.

### E3 — Footer coverage + the five `query_inclusion` behaviors

- Footer extends to `graph n/m · chunks n/m · crystals n/m · units n/m` — honest coverage accounting, same contract as today (no silent truncation).
- Behavior matrix from fixtures: `excluded` (chatter/noise never appears), `explicit_memory_only` (appears only with the flag), `latest_only` (superseded c4 fact never surfaces, its successor does), `corrected_only` (c7 same), `surface_conflict` (both c6 units appear, marked `[conflict]`, neither suppressed).
- Conflict marking is read-side only — consolidation remains the only writer.

### E4 — Reinforcement (resolved: bookkeeping-only — owner decision #1)

- On memory recall in query output: bump `recall_count`, set `last_recalled_at` (write-behind, same transaction discipline as store ops; never blocks the read path on failure).
- **Counters do NOT feed ranking** — the E1 recency factor uses `updated_at` only. Enabling the feedback loop (recall counters influencing rank) is a separate future decision that requires eval coverage first; recorded under Owner decisions.

### E5 — Acceptance: lifecycle query-inclusion harness + eval regression

- Extend `tests/test_lifecycle_acceptance.py`: full pipeline (import → triage → extract → consolidate → query with/without `--include-memory`) asserting every fixture's `query_inclusion` expectation — this is the "small golden lifecycle fixture suite" spec_02 demands beyond unit tests.
- Memory-fusion quality check (spec metric): for a fixture query whose answer lives in source chunks, assert the answer-bearing chunk still appears with memory on (the no-crowding-out acceptance, as a test not a claim).
- Eval regression on every milestone (E0 contract, strict-wrapper form); final pre-merge: offline 24/26 + Gemini 26/26 re-verification (owner-approved, agent-triggered when the API key is available).
- E5 also asserts the project-scoping behavior: a second project's memories never appear without `--all-projects`.

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
- Gates per milestone: `uv run pytest` + ruff + strict mypy green; coverage ≥ 80%; offline eval exactly 24/26 @ 4000 on every milestone commit **via the E1 strict wrapper** (plain `--check-gates` skips the gate in offline mode — do not rely on it).
- Subagents stop and escalate on judgment calls beyond the brief (notably the E4 reinforcement decision and any ranking-weight change).

## Owner decisions (resolved 2026-06-12, at plan review)

1. **E4 reinforcement: bookkeeping-only.** Write `recall_count`/`last_recalled_at` on recall; do NOT feed them into ranking — the recency factor uses `updated_at` only for now. Turning the feedback loop on (recall counters influencing rank) is a separate future decision requiring eval coverage first.
2. **`memory_share` default: 0.15** — corrected from this plan's earlier 0.2 proposal, which was an undeclared deviation: spec_02 defines `memory_share (0.15)` as the `MemboxConfig` default (line ~741) and the prose recommends starting conservative at 0.15 (line ~824). 0.2 becomes a calibration candidate: raise only after E5's no-crowding-out test passes with margin at the larger share. (300 of the 2000 default budget; 600 of the 4000 eval budget.)
3. **Crystal tier: relevance-first boost (×1.5) + equal-score tie-break, NOT lexicographic admission.** This is an explicit owner arbitration of a spec-internal tension: spec_02 line ~825 says the pool "should admit crystals before active units" (literal reading = admission priority), while line ~832 mandates "a highly relevant moderate-score unit should beat an irrelevant high-score one" (relevance can win). Resolution: ranking is `relevance × stored_score × recency × tier_boost` with `MEMORY_CRYSTAL_BOOST = 1.5`; "admit crystals before units" is interpreted as the tie-break — at equal final score the crystal is admitted first. Rationale: literal admission priority lets accumulated off-topic crystals statically fill the pool and crowd out answer-relevant units, which contradicts the line-832 design intent and degrades irreversibly once reinforcement ever feeds ranking. E1's implementation carries this note next to the constant; spec_02 line 825 should gain a clarifying parenthetical "(at equal rank)" in a follow-up spec commit referencing this decision.
