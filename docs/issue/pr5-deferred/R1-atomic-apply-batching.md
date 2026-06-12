# R1 — Atomic apply batching for `distill --apply`

**Severity**: Major (correctness; partial apply leaves the DB in a state that the next apply cannot cleanly recover from).
**Origin**: PR #5 review (CodeRabbit thread resolved out of scope of the merge).

## Problem

The Phase F distill `--apply` path is read-only by design (the help text says `Reserved for a later Phase F apply path`). When the future apply path is implemented, the natural shape is "for each candidate, mark supersession/retract/restart and write to memory_units" — but if any single transition fails partway through, the DB will be left with a half-applied batch: some candidates marked, others not, with no rollback.

The Trace → Unit → Crystal design assumes that consolidation transitions are observable but reversible only through `supersede` / `restore`. A partial apply would create asymmetric state that the next `consolidate --apply` cannot reason about.

## Proposed fix

Wrap the apply path in a single SQLite transaction with `BEGIN IMMEDIATE`:
- Stage the transitions in a working set in memory.
- Validate every transition's preconditions (target unit exists, supersession graph is acyclic, etc.).
- Apply in one transaction; on any error, ROLLBACK and surface the failure with the first offending unit id.
- Return the same shape on partial failure as on success: `applied: N, skipped: M, failed: K` with the unit ids for each.

## Acceptance criteria

- A test injects a forced failure on the 7th of 10 candidate units and asserts: the 6 earlier transitions are NOT visible after the failure (transaction rolled back), the error message names the 7th unit, and a follow-up `--apply` on the same 10 candidates succeeds cleanly.
- Coverage of the transaction-rollback path is at least the same as the happy path.

## Sequencing

R1 should land **before** R2 / R3 / R4. Without atomic apply, the gate changes in R2–R4 are exercised against a brittle apply path that may silently leave the DB in inconsistent state during testing.
