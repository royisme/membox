# R2 — FTS conflict pairing in the lifecycle gate

**Severity**: Major (data quality; surfaces contradictions the deterministic gate currently misses).
**Origin**: PR #5 review (CodeRabbit thread resolved out of scope of the merge).

## Problem

The FTS5 index over memory_units and relations surfaces textual conflicts the deterministic gate does not: two units about the same fact phrased differently, a relation that contradicts an earlier one, a unit that re-states an archived crystal. The current gate (v3) runs a deterministic pass only; FTS hits are not paired into candidate conflicts that `consolidate --dry-run` would surface.

The `consolidate` command already exposes a "conflicts" surface, but it is populated from the same deterministic pass. Adding FTS pairing widens the surface to catch paraphrastic conflicts.

## Proposed fix

- In the gate (wherever `consolidate --dry-run` builds its conflict list), add a second pass that runs an FTS5 query per active unit's text and pairs top-K candidates with cosine-overlap or simple token overlap.
- Pairing is a candidate, not a hard conflict: the operator still promotes via `consolidate --apply`, and the FTS pairing is one input among others.
- New FTS-pair rows are stored separately from deterministic conflict rows (for example, `plan.fts_pairs` or `fts_pair_rows`) so the dry-run output is uniform without converting FTS pairs into hard conflict IDs.
- `build_consolidation_plan()` must only convert deterministic `plan.conflicts` into `conflict_ids`. FTS pairs must not suppress unrelated promotions, candidates, demotions, or supersessions; render and serialize them alongside conflicts where operator output needs the combined review surface.

## Acceptance criteria

- A test corpus with two paraphrases of the same fact produces an FTS-pair row pointing at both units.
- The dry-run output of `consolidate --dry-run` lists the FTS-pair row in the same shape as deterministic conflict rows.
- Apply path does not auto-resolve FTS pairs (they remain candidates for human review); only the explicit `consolidate --apply` operator action promotes them.

## Sequencing

Land after R1 (atomic apply) so that the gate is exercised against a safe apply path. Land before R3 (LLM comparator) so the comparator has the FTS-pair input to score.
