# R4 — Gate v4: integrate R1–R3 into the shipped gate

**Severity**: Major (gate hardening; codifies the post-R1–R3 invariants).
**Origin**: PR #5 review (CodeRabbit thread resolved out of scope of the merge).

## Problem

The current shipped gate is v3 (per `docs/spec/spec_02_memory_lifecycle.md` and the `gate v3` reference in the Stabilization S1 PR description). v3 is deterministic-only. After R1, R2, and R3 land, the gate has new inputs (atomic apply semantics, FTS pairing, optional LLM comparator) that v3 does not use. A gate version bump is needed to make the new behavior the default and to lock in the test suite against drift.

## Proposed fix

- Bump the gate from v3 to v4 in `docs/spec/spec_02_memory_lifecycle.md` and the gate implementation.
- v4 = v3 + R2 (FTS pairing always run) + R3 (LLM comparator when configured) + R1 invariants (apply is atomic).
- The `--no-llm` knob (already on `process`; mirror it on `consolidate` if not present) explicitly opts out of R3, so CI and offline-mode users are unaffected.
- Update `tests/test_lifecycle_acceptance.py` to assert v4 invariants alongside v3's.

## Acceptance criteria

- v4 is the default; `--gate v3` is available for one release cycle as an escape hatch, then removed.
- The lifecycle spec document is updated to mark v3 as superseded and v4 as the current gate.
- CI runs the v4-no-LLM path; the offline eval runs the v4-LLM path against the corpus; both pass.

## Sequencing

R4 is the last of the deferred items. It cannot land before R1, R2, and R3 because it integrates them. After R4 lands, the lifecycle track is "frozen at v4" and future changes go through the normal deprecation cycle.
