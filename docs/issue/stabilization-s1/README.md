# Stabilization S1 — Dogfooding Issue Backlog

**Source**: end-to-end smoke run on `main` (commit `01258cc`, post PR #5), 2026-06-12, against an isolated temp SQLite with a hand-written `membox-capture` JSONL fixture (`/tmp/membox-capture-sample.jsonl` — 1 session header, 4 messages, 1 event) and a corrupted copy of the same fixture. Discovery process and raw output in repo memory `stabilization-s1-defects.md`.

**Severity legend**: Major = blocks a Stabilization Track work item or corrupts user-visible data; Minor = UX/copy/wording that does not block but should be fixed before v0.1.0.

**Ordering rationale**: D3 (lifecycle chain breaks) is the most upstream — without it, S1 cannot produce any memory units, so D1 / D2 / FTS work / distill work / PR5-deferred review items all have no substrate to verify against. D1 + D2 share a file with D6; D4 is independent; D5 is one-line.

| ID | Severity | Title | File |
|---|---|---|---|
| [D1](./D1-history-around-msgid-not-found.md) | Major | `membox history around <msg_id>` returns "no such message" for an id returned by SQLite | `src/membox/cli/commands/history.py` |
| [D2](./D2-history-search-no-hits-on-known-text.md) | Major | `membox history search <q>` returns "No history hits" for words that exist in the imported text | `src/membox/cli/commands/history.py` (likely) |
| [D3](./D3-memory-extract-creates-zero-units.md) | Major | `membox memory extract --apply` reports "Created 0 units" even when triage produced N trace rows — lifecycle chain breaks in the offline path | `src/membox/core/memory_extractor.py` (or equivalent) |
| [D4](./D4-corrupt-jsonl-silently-loses-records.md) | Major | Corrupted JSONL line is silently absorbed; importer reports success with reduced counts, no warning | `src/membox/core/history_import.py` |
| [D5](./D5-distill-apply-error-message-stale.md) | Minor | `membox distill` without `--dry-run` errors with stale message inconsistent with `--apply` help | `src/membox/cli/commands/distill.py` |
| [D6](./D6-session-root-error-msg-conflates-cases.md) | Minor | `MEMBOX_SESSION_ROOT` required-error conflates "no path" with "no session_root" | `src/membox/cli/commands/history.py` |

**Execution order proposed for S2 / S3 work** (informs PR sequencing, not contract):

1. D3 — restores the lifecycle chain. Without it S1 has no substrate.
2. D1 + D2 — read-path regressions on the same CLI file; one fixup PR after the read path is traced.
3. D4 — independent; small surface change (return + display skipped_lines).
4. D5 / D6 — copy fixes; group with any other UX cleanups.

Cross-reference: [PR5-deferred review items](../pr5-deferred/README.md) lists the four review items carried out of PR #5 into the Stabilization Track.
