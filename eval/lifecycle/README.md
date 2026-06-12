# Lifecycle Eval Fixtures

Committed synthetic fixtures for Agent Memory Lifecycle Phase C.

Unlike `eval/corpus/`, this directory is safe to commit: every transcript is
synthetic and uses the normalized `membox-history-jsonl` format. The fixtures
exist before migration 8 so triage and extraction work can be calibrated
against stable expected outcomes instead of ad hoc examples.

Files:

- `history/*.jsonl` — synthetic trace sessions importable by `membox history import --format membox-history-jsonl`.
- `expectations.yaml` — gold expectations for triage, extraction, activation,
  source references, future Phase D status, and query inclusion/exclusion.
