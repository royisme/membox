# R3 — LLM comparator for candidate memory units

**Severity**: Major (recall quality; reduces gate noise on borderline candidates).
**Origin**: PR #5 review (CodeRabbit thread resolved out of scope of the merge).

## Problem

The deterministic gate occasionally lets through low-quality units (rephrasings of archived content, paraphrases of existing crystals with no new information) and occasionally drops borderline ones that a human would have kept. A second LLM pass that re-scores candidates can sharpen the boundary.

This is the first of the LLM-touching deferred items, and the design needs to be careful: LLM calls are slow and expensive, so the comparator must be a focused, batched pass — not a per-candidate call.

## Proposed fix

- Add a `LLMComparator` protocol in the same shape as the existing `LLMExtractor` (per `docs/agent/02-architecture-boundaries.md`'s protocol conventions).
- The gate, when LLM is configured AND the comparator is injected, sends a batch of (candidate, surrounding units) tuples to the comparator and receives a re-score per candidate.
- Candidates that score below threshold after LLM re-scoring are dropped from the apply path; they remain in triage for inspection.
- The no-LLM path (current behavior) is unchanged. LLM is strictly an upgrade.

## Acceptance criteria

- With LLM disabled, behavior is byte-identical to current gate (no regressions in `tests/test_lifecycle_acceptance.py`).
- With LLM enabled, a hand-curated set of borderline candidates shows >80% agreement with human labels.
- Coverage in CI is unchanged: CI runs the no-LLM path; the LLM path is exercised by the offline eval harness, not CI.

## Sequencing

Land after R1 (atomic apply) and R2 (FTS pairing) so the comparator scores against the full set of gate inputs. Land before R4 (gate v4) so the comparator can be one of the gate's inputs in v4.
