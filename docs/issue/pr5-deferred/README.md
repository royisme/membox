# PR #5 — Deferred Review Items

**Source**: items that CodeRabbit or the owner flagged in the PR #5 review cycle but were intentionally carried out of the squash-merge into the Stabilization Track rather than blocking merge. Recorded here so subsequent PRs can reference them by ID.

**Why deferred, not dropped**: each of these was identified during PR #5 review, and a code change was discussed, but the consensus was that landing the PR first (with the Distill workflow and the skill file) and addressing the items in a follow-up Stabilization Track PR was lower-risk than holding the PR open. The HANDOFF and the lifecycle spec reference these items; this directory is the canonical pointer.

| ID | Source | Title | Notes |
|---|---|---|---|
| [R1](./R1-atomic-apply-batching.md) | PR #5 review | Atomic apply batching | Improve `distill --apply` so multi-step transitions are rolled back on partial failure. |
| [R2](./R2-fts-conflict-pairing.md) | PR #5 review | FTS conflict pairing | Pair conflicting entities/relations surfaced by the FTS index in the lifecycle gate. |
| [R3](./R3-llm-comparator.md) | PR #5 review | LLM comparator | A second LLM pass that re-scores candidate memory units to reduce gate noise. |
| [R4](./R4-gate-v4.md) | PR #5 review | Gate v4 | Next iteration of the lifecycle quality gate; the existing v3 is the current shipped baseline. |

**Execution order proposed**: R1 (correctness, makes apply safe) → R2 (data quality) → R3 (recall quality) → R4 (gate tightening). R1 should land before any of R2–R4 so that downstream work is exercised against a safe `apply`. R3 and R4 are best done after the offline deterministic extract path (Stabilization D3) is fixed so that gates have a substrate to evaluate.

Cross-reference: [Stabilization S1 backlog](../stabilization-s1/README.md) for first-priority items that may need to land before R1–R4 can be verified end-to-end.
