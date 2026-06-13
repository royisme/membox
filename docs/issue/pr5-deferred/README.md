# PR #5 — Deferred Review Items

**Source**: items that CodeRabbit or the owner flagged in the PR #5 review cycle but were intentionally carried out of the squash-merge into the Stabilization Track rather than blocking merge. Recorded here so subsequent PRs can reference them by ID.

**Current status**: all four deferred items have been folded into the current
roadmap state. The implementation now has atomic consolidation apply,
FTS-style review pairs, an injectable comparator protocol with offline replay
coverage, and `heuristic-v4` as the current gate version.

**Why this directory remains**: each item was identified during PR #5 review,
and this directory preserves the issue-level context and acceptance criteria
behind the Stabilization Track updates.

| ID | Source | Title | Notes |
|---|---|---|---|
| [R1](./R1-atomic-apply-batching.md) | PR #5 review | Atomic apply batching | Done — `transition_memory_units_atomically` covers atomic consolidation apply. |
| [R2](./R2-fts-conflict-pairing.md) | PR #5 review | FTS conflict pairing | Done — FTS pairs are emitted as review-only rows, separate from hard conflicts. |
| [R3](./R3-llm-comparator.md) | PR #5 review | LLM comparator | Done — `LLMComparator` is injectable and covered by offline replay tests. |
| [R4](./R4-gate-v4.md) | PR #5 review | Gate v4 | Done — `heuristic-v4` is current; `heuristic-v3` remains the temporary escape hatch. |

**Execution order used**: R1 (correctness, makes apply safe) → R2 (data
quality) → R3 (recall quality) → R4 (gate tightening).

Cross-reference: [Stabilization S1 backlog](../stabilization-s1/README.md) for the dogfooding defects that were fixed before this status update.
