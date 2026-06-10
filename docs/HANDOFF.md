# Membox — Session Handoff

> Single source of truth for cross-session context. Read at session start; update before ending.

**Last updated**: 2026-06-10 (session 4 — M6 async ingestion queue implemented)
**Current phase**: Phase 7.5 in progress (M1-M3 + M6 done; M4/M5 not started). Phase 8 (AST) deliberately deferred until after 7.5.

---

## What's been done

### Sessions 1-2 (through 2026-06-09)
Scaffolding, spec/roadmap, and Phases 1-7 (skeleton → storage → normalization → disambiguation → BFS → concurrency → OpenAI providers). All since merged to main. Details: `git log` and `docs/roadmap.md` checkboxes.

### Session 3 (2026-06-09 → 2026-06-10)
- **Phase 7.5 "Memory Quality Validation" designed and specced** (`docs/spec.md` §3.6-3.9, roadmap M1-M6): validate real memory behavior on handoff-document corpora before building more features.
- **M1 merged**: `eval/corpus/` (9 real handoff docs snapshotted from local projects — frozen test data, do not update) + `eval/gold.yaml` (26 gold QA: 15 single-hop / 7 multi-hop / 4 temporal, ~31% Chinese).
- **M2 merged**: schema v2 (documents metadata: project/source_path/section/doc_date/version; meta table), markdown section chunking (fence-safe), idempotent versioned re-ingest, `--project`/`--doc-date` CLI. Fixed: project default now walks up to git root (was: parent dir, which yielded "docs").
- **M3 merged**: schema v3 (relation embeddings as float32 BLOB; FTS5 external-content table + sync triggers), spec §3.7 context-budgeted retrieval (composite scoring, greedy token-budget knapsack, subject-grouped compact output with mandatory coverage footer — now the DEFAULT query path), `RetrievalConfig`, meta-table embedder guard, `scripts/eval_memory.py`.
- **Hardening from first real runs**: oversized-section sub-chunking + per-chunk failure isolation + explicit `max_tokens` (a 7k-token section had crashed the whole corpus ingest); extraction prompt direction example (small models swapped subject/object).
- **First real baseline evaluation completed** (gemma-4-E2B + qwen3-embedding via Ollama): **11.5% hit rate (3/26)**. Failure analysis below — this number is the point of 7.5, not a setback.
- **M6 "Asynchronous Ingestion Queue" specced** (branch `feature/phase-7.5-m6-queue-spec`, commit `fe20a53`): user identified that synchronous ingestion blocks callers for minutes; design = SQLite queue table + auto-spawned short-lived worker (lease in meta table, exits when drained — reconciled with no-daemon constraint).

### Session 4 (2026-06-10)
- **M6 spec merged to main**, then **M6 implemented** (branch `feature/phase-7.5-m6-async-queue`): schema v4 `ingest_queue` table; `QueueOps` store mixin (enqueue/claim/done/failed/retry, JSON `worker_lease` in meta with 60s TTL, expired-lease takeover resets stale `processing` rows); `core/worker.py` (`drain_queue` + `spawn_worker` detached subprocess logging to `<db>.worker.log`); `MemoryAgent.enqueue`/`enqueue_file`/`ingest_content` (the latter extracted from `ingest_file` so the worker reuses the exact M2/M3 pipeline); CLI `ingest`/`ingest-file` async by default with `--sync`/`--no-spawn`, new `membox process` (`--retry-failed`, max 3) and `membox queue` commands; query output appends "(N ingest(s) pending — results may be incomplete)" when the queue is non-empty. `python -m membox.cli` entry added for the worker spawn. All M6 acceptance criteria ticked in roadmap; 351 tests, ~93% coverage, mypy strict + ruff clean. End-to-end verified: real ingest-file enqueues in <100ms, detached worker drains and exits, lease released.
- Note: `scripts/eval_memory.py` already used the synchronous API (`agent.ingest_file`), which is unchanged — determinism preserved without modification.

---

## Current state

- **main**: Phases 1-7 + Phase 7.5 spec (incl. M6 spec) + M1 + M2 + M3 + eval/model fixes all merged.
- **Pending branch**: `feature/phase-7.5-m6-async-queue` (M6 implementation) — awaiting user review/merge. 351 tests, ~93% coverage, mypy strict + ruff clean.
- **Old phase 1-7 feature branches and `develop` still exist** but are historical; main is authoritative. Safe to delete after confirmation.
- **Baseline eval DB**: `/tmp/membox-eval-m3.db` (51 chunks ingested, 600 entities / 321 relations, 7 chunks failed on 2048-token completion limit). Rerun: `uv run python scripts/eval_memory.py --db <path>`.

### Locked architectural decisions
- **Single global DB** (`~/.membox/membox.db` default; `--db` > `MEMBOX_DB` env > default). No per-project DBs, no registry, no ATTACH federation. `documents.project` column scopes; entities/relations are global for cross-project identity.
- **Local provider defaults** (recalibrated 2026-06-10 after previously-chosen models were deleted from Ollama): extraction `gemma-4-E2B:latest` (structured-output path works; bare prompt truncates), embedding `qwen3-embedding:latest` 1024-dim, disambiguation threshold 0.72 (same-entity ≥0.763, diff-entity ≤0.680 measured). Overridable via `MEMBOX_EVAL_*` env vars. OpenAI default threshold stays 0.85.
- **Read path has no LLM**: pruning is ranking + budgeting only (spec §3.7). Compact budgeted output is the default; silent truncation/staleness forbidden (coverage footer).
- **Write path goes async** (M6 spec): enqueue in ms, transient worker materializes the graph; eventual consistency surfaced in query footer. No resident daemon.
- No external services / hosted vector DBs; SQLite WAL, FK ON, per-thread connections, RLock (unchanged).
- `eval/corpus/` is frozen: gold answers (incl. temporal q23) depend on the snapshotted content. Re-snapshotting is an M4-era task paired with updating temporal gold answers.

---

## Open questions / decisions needed

1. **M6 implementation branch review** — merge `feature/phase-7.5-m6-async-queue`? (The docs-only spec branch was merged to main this session.) Then order is retrieval fallback → extraction quality → M4 → M5.
2. **Retrieval fallback design (proposed, unconfirmed)**: baseline's biggest killer is seed-resolution failure (~half of misses returned only an empty footer; BM25 never fires because it only scores BFS-reached triples). Proposal: when seeds resolve to nothing (or graph recall is empty), fall back to direct FTS5 search over evidence chunks. Awaiting user confirmation.
3. **Extraction quality on small local models**: 258/600 entities typed "Unknown"; some garbage whole-sentence entity names; 7 chunks still exceed 2048 completion tokens. Options: stricter prompt + few-shot, retry with smaller sub-chunks, or stronger local model. Not yet decided.
4. **Ollama throughput**: sync ingest of 51 chunks ≈ 1-2h (serial: per-chunk generation + ~900 single embedding calls). Batch the embed calls (Ollama API supports list input) as part of M6 worker work.
5. Old branches + `develop` cleanup — delete after user confirms.
6. Phase 8 (tree-sitter), Phase 9 (skill file), Phase 10 (release 0.2.0) — queued behind 7.5. Phase 9's skill design = query at session start, async ingest at session end (depends on M6).

---

## Next concrete steps

1. User reviews + merges `feature/phase-7.5-m6-async-queue` (M6 implementation — done this session).
2. Implement FTS fallback seeding (pending confirmation of #2 above); rerun baseline, expect large single-hop/multi-hop recovery.
3. Extraction-quality iteration + batched embeddings; rerun eval.
4. M4 supersession semantics (schema migration: `relations.superseded_by`), then re-snapshot corpus + update temporal gold answers.
5. M5 close-the-loop: `membox ingest-file docs/HANDOFF.md` end-to-end; gate: ≥80% hit rate within 2000-token budget (temporal 100%).

---

## Notes / scratchpad

- Acceptance gates for 7.5 overall: hit ≥80% in default 2000-token budget; temporal 100%; coverage ≥80%.
- eval per-question output includes token estimate; mean was 275 tokens at baseline.
- ResourceWarning re unclosed SQLite connections in CLI tests — cosmetic, future `close()` cleanup.
