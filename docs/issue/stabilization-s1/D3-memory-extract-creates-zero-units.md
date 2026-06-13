# D3 — `membox memory extract --apply` reports "Created 0 units" even when triage produced N trace rows

**Severity**: Major (lifecycle chain break in the offline / no-LLM path; blocks Stabilization Track end-to-end).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# 1. Import a session (smoke fixture: 4 messages, 1 event).
uv run membox history pull --db "$DB" --adapt membox --project smoke "$SAMPLE"
# 2. Triage succeeds.
uv run membox memory triage --db "$DB" --project smoke --apply
#   → Triaged 5 trace rows.
#   → Wrote 5 triage rows.
# 3. Extract is supposed to seed memory unit candidates from those triage decisions.
uv run membox memory extract --db "$DB" --project smoke --apply
#   → Created 0 units.
# 4. Downstream surfaces confirm the gap.
uv run membox memory list --db "$DB" --project smoke
#   → No memory units.
```

## Expected

`memory extract --apply` creates memory unit candidate rows from the triage decisions that the deterministic gate marked eligible. In the smoke run, with 5 triaged trace rows, the count should be > 0 (the exact number depends on the gate's decisions, but it should not be zero unconditionally).

## Actual

`Created 0 units.` Every time, even when the deterministic gate has marked the trace rows eligible. Downstream `query --include-memory` and `distill` then see "No memory units", which is the lifecycle chain's terminal symptom.

## Why this matters

This is the **central gap** for Stabilization S1. Without deterministic extraction producing memory units, the entire `Trace → Unit → Crystal` design has no substrate to verify against in an offline environment. Every subsequent S1 deliverable — verifying D1/D2 read paths, evaluating D4 robustness, running D5/D6 polish, and the PR5-deferred review items (atomic apply batching, FTS conflict pairing, LLM comparator, gate v4) — assumes units can be created. The offline path is also the path used in CI; this defect means CI never exercises the "happy path" through extract.

## Likely cause

Verify by reading the code, not by guessing:

1. **`memory extract` is hard-gated on an LLM call.** The deterministic extractor is a stub that requires the OpenAI extractor; with no `OPENAI_API_KEY` it produces nothing. The `--no-llm` knob exists on `process` but not on `extract`.
2. **The extractor iterates a different source than triage wrote to.** Triage persists to `memory_triage`, extract reads from a different (perhaps LLM-generated) source.
3. **A filter in the extractor drops deterministic-decision rows.** The extractor may be looking only for `decision="needs_llm"` rows and ignoring `decision="auto_create"`, leaving the offline path empty.

## Suggested fix

- Identify the actual gating condition (LLM requirement, table mismatch, or filter).
- Make the deterministic extract path genuinely deterministic: it should consume triage decisions marked eligible and produce candidate `memory_units` rows without contacting the LLM.
- The LLM path remains the upgrade; the deterministic path is the substrate.
- Add a test that exercises `triage --apply` then `extract --apply` on a fixture and asserts a non-zero unit count.

## Acceptance criteria

- After `triage --apply` on the smoke fixture, `extract --apply` creates ≥ 1 memory unit (or returns a clear "no eligible rows" message if the gate legitimately filtered everything — but that case should be the exception, not the rule).
- The deterministic path is reachable without `OPENAI_API_KEY` and without an `--no-llm` flag; the LLM path is opt-in for upgrade only.
- A test in the lifecycle acceptance suite pins the deterministic path's contract.

## Why this is the first item in S2

D3 unblocks everything: once units can be created offline, D1/D2/FTS work/distill work/PR5-deferred items all have a substrate to verify against. Fix D3 first.
