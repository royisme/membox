# Stabilization S1 — Dogfooding Issue Backlog

**Source**: end-to-end smoke run on `main` (commit `01258cc`, post PR #5), 2026-06-12, against an isolated temp SQLite with a hand-written `membox-capture` JSONL fixture (`/tmp/membox-capture-sample.jsonl` — 1 session header, 4 messages, 1 event) and a corrupted copy of the same fixture. Discovery process and raw output in repo memory `stabilization-s1-defects.md`.

**Severity legend**: Major = blocks a Stabilization Track work item or corrupts user-visible data; Minor = UX/copy/wording that does not block but should be fixed before v0.1.0.

**Current status**: current code has regression coverage for D1–D6. This
directory remains as the discovery record from the initial dogfooding pass.

**Ordering rationale used**: D3 (lifecycle chain breaks) was the most upstream
item — without it, S1 could not produce any memory units, so D1 / D2 / FTS work
/ distill work / PR5-deferred review items had no substrate to verify against.
D1 + D2 shared a file with D6; D4 was independent; D5 was one-line.

| ID | Severity | Title | File |
|---|---|---|---|
| [D1](./D1-history-around-msgid-not-found.md) | Major | Done — `history around <id_from_sqlite>` resolves the exact message id | `tests/test_history_cli.py` |
| [D2](./D2-history-search-no-hits-on-known-text.md) | Major | Done — search path is covered, and scoped misses explain `--project` / `--all-projects` | `tests/test_history_cli.py` |
| [D3](./D3-memory-extract-creates-zero-units.md) | Major | Done — deterministic triage → extract creates units without an LLM | `tests/test_lifecycle_acceptance.py` |
| [D4](./D4-corrupt-jsonl-silently-loses-records.md) | Major | Done — malformed JSONL lines are reported and produce a visible failure | `tests/test_history_cli.py`, `tests/test_history_importers.py` |
| [D5](./D5-distill-apply-error-message-stale.md) | Minor | Done — `distill` uses the `--apply is not yet implemented` wording | `tests/test_distill.py` |
| [D6](./D6-session-root-error-msg-conflates-cases.md) | Minor | Done — missing session root error lists path/env/flag options | `tests/test_history_cli.py` |

**Execution order used for S2 / S3 work** (historical record):

1. D3 — restores the lifecycle chain. Without it S1 has no substrate.
2. D1 + D2 — read-path regressions on the same CLI file; one fixup PR after the read path is traced.
3. D4 — independent; small surface change (return + display skipped_lines).
4. D5 / D6 — copy fixes; group with any other UX cleanups.

Cross-reference: [PR5-deferred review items](../pr5-deferred/README.md) lists the four review items carried out of PR #5 into the Stabilization Track.
