# Membox — Session Handoff

> Single source of truth for cross-session context. Read at session start; update before ending.

**Last updated**: 2026-06-11 (session 9 — lifecycle design accepted, test corpus CI fix)
**Current phase**: Phase 7.5 retrieval quality gate complete (24/26 verified, merged to main at `77c4090`). The next-stage agent memory lifecycle design (Trace → Unit → Crystal) is **accepted at v2.3** on `feature/lifecycle-design-v2` — all product and engineering decisions owner-confirmed, no remaining open items blocking Phase B implementation.

### Pending user decisions before next work item
- **Merge `feature/lifecycle-design-v2` into main** (see session 9 below). After that, Phase B (history trace index) is unblocked and becomes the next implementation track.
- `scripts/eval_memory.py` has a working-tree edit changing the default Gemini extraction model `gemini-3-flash-preview` → `gemini-3.1-flash-lite` (origin unclear, made before session 8, used in session 8 evals). **Commit, revert, or pin differently?** — only the user can decide.
- Stale `feature/*` branches (phase 1-7, phase 7.5 sub-branches) and `develop` — safe to delete after user confirms.

### New conventions worth remembering across sessions
- **Merge into main uses `merge --no-ff` with a `merge: <theme>` message**, then a separate commit for handoff/design-doc syncs on the feature branch. See `git log main --merges` for examples. No PR/CODEOWNERS flow is required.
- **When asking a subagent to "merge and run tests", do NOT chain them in one Bash call with `run_in_background: true`** — the output buffer hides pytest results and forces poll loops. Pattern: merge first (seconds), then a separate `pytest` call. Lesson learned in session 8.
- **Long-running commands that print only at the end** (full eval, ingest) can be run in the background, but a "wait for terminal output" pattern via `until grep ...; do sleep N; done` blocks the shell and is rejected by the harness. Use `run_in_background: true` and let the harness notify on exit.

---

### Session 9 (2026-06-11) — Lifecycle design accepted + test corpus CI fix
- **Agent memory lifecycle design accepted at v2.3** on `feature/lifecycle-design-v2` (branch ahead of main, 4 commits). Three review passes; every product and engineering decision is owner-confirmed. See `docs/design/agent-memory-lifecycle.md` Revision Log for the full v0→v1→v2→v2.1→v2.2→v2.3 trail. The doc is the single source of truth for the next-stage design and merges into `docs/spec.md`/`docs/roadmap.md` only after the owner accepts it on main.
- **Owner-confirmed product decisions (v2.2)**:
  - No Membox-managed blob storage in any phase. Tool outputs already live in the upstream session jsonl (Codex/Claude/MiMo retain it); Membox stores a capped preview plus a `payload_locator` (identity-based, not byte offset) and re-reads the upstream file on demand via `membox history fetch <id>`. Supersedes the v1/v2 truncation+blob proposal.
  - Markdown export is one-way only; human-edited Markdown cannot re-import.
  - Initial label set (11 labels orthogonal to unit types): `architecture/storage/retrieval/cli/testing/tooling/workflow/conventions/dependencies/performance/security`.
  - HOT working-state tier excluded from this track but parked in `docs/roadmap.md` "Future Tracks" as a future standalone design.
- **Owner-confirmed engineering decisions (v2.3)**: `dream` not in public CLI; cross-process write coordination via per-project `lifecycle_lease` (reusing the existing `worker_lease` pattern from `core/worker.py`, not the in-process RLock); dedup anchored on source identity with `content_hash` as secondary; decay executed by `memory consolidate --apply`; archive/restore preserves prior status from `memory_unit_status_log`; "independent sources" means distinct sessions; import-time secret redaction on by default; lifecycle eval fixtures are the FIRST deliverable of Phase C; `history`/`memory search` default to current project, `--all-projects` required for cross-project.
- **Theory-grounded additions**: memory-pool ranking is `relevance × importance × recency` (not static scores); `recall_count`/`last_recalled_at` on `memory_units` for future threshold calibration; lifecycle eval moved from "before Phase E" to "first deliverable of Phase C" because C/D acceptance metrics are measured against it.
- **Roadmap synced**: `docs/roadmap.md` now has a "Future Tracks" section that names the lifecycle design (still requires owner sign-off on main before promotion to Phases A–F) and the HOT state tier.
- **CI fix in `tests/test_eval_corpus.py`** (commit `4bfb427`, "add new features spec"): added a `requires_corpus` fixture that skips the two corpus-dependent tests when `eval/corpus/` is absent. `eval/corpus/` is **gitignored** (contains private handoff docs from local projects, not safe to share); without this fixture the tests hard-failed on any CI runner. This is the same `f6c8a99` "working-tree cleanup" content (the model default swap + lifecycle design draft) — the test change rides along with that theme.
- Persistent memory updated (`lifecycle-design-accepted`) so future sessions don't re-derive the design or the no-blob-storage decision.

---

### Sessions 1-2 (through 2026-06-09)
Scaffolding, spec/roadmap, and Phases 1-7 (skeleton → storage → normalization → disambiguation → BFS → concurrency → OpenAI providers). All since merged to main. Details: `git log` and `docs/roadmap.md` checkboxes.

### Session 3 (2026-06-09 → 2026-06-10)
- **Phase 7.5 "Memory Quality Validation" designed and specced** (`docs/spec.md` §3.6-3.9, roadmap M1-M6): validate real memory behavior on handoff-document corpora before building more features.
- **M1 merged**: `eval/corpus/` (9 real handoff docs snapshotted from local projects — frozen test data, do not update) + `eval/gold.yaml` (26 gold QA: 15 single-hop / 7 multi-hop / 4 temporal, ~31% Chinese).
- **M2 merged**: schema v2 (documents metadata: project/source_path/section/doc_date/version; meta table), markdown section chunking (fence-safe), idempotent versioned re-ingest, `--project`/`--doc-date` CLI. Fixed: project default now walks up to git root (was: parent dir, which yielded "docs").
- **M3 merged**: schema v3 (relation embeddings as float32 BLOB; FTS5 external-content table + sync triggers), spec §3.7 context-budgeted retrieval (composite scoring, greedy token-budget knapsack, subject-grouped compact output with mandatory coverage footer — now the DEFAULT query path), `RetrievalConfig`, meta-table embedder guard, `scripts/eval_memory.py`.
- **Hardening from first real runs**: oversized-section sub-chunking + per-chunk failure isolation + explicit `max_tokens`; extraction prompt direction example (small models swapped subject/object).
- **First real baseline evaluation completed** (gemma-4-E2B + qwen3-embedding via Ollama): **11.5% hit rate (3/26)**.

### Session 4 (2026-06-10) — M6
- M6 spec merged to main, then **M6 implemented** (commit `030f2f9`): schema v4 queue, lease-guarded transient worker, CLI async-by-default with `--sync`/`--no-spawn`, `membox process`/`queue` commands, pending-ingests staleness note on query output. End-to-end verified.
- Materialization extracted into `MemoryAgent.ingest_content` so worker and sync paths share one pipeline.

### Session 5 (2026-06-10) — FTS fallback
- **FTS fallback seeding implemented** (commits `a0f3c12`, `d88ac72`) — resolves open question #2. When seed resolution finds no entities OR `scored_query` returns no triples, `compact_query` falls back to direct FTS5 BM25 over `documents.content`. Design: OR-of-tokens MATCH (`_fts5_or_query`); version dedup by `(source_path, section)`; honest footer `(returned 0/0 triples, K/M FTS chunks, ~X/Y tokens…)`; `RetrievalConfig.fts_fallback_k` (initial default 5, later calibrated to 10 in session 7, `0` disables); `--project` applies.
- New code: `RetrievalOps.fts_fallback_chunks` / `fts_fallback_output` (`retrieval.py`), `MemoryAgent._fts_fallback` (`agent.py`). 21 new tests; 372 total green.

### Session 6 (2026-06-10 → 2026-06-11) — Online eval + gap analysis
- **M6 + FTS fallback merged to main** (commits `2d57c45`, `d88ac72`).
- **Online eval pipeline added** (branch `feature/phase-7.5-eval-gemini`, commits `dc531e0` + `75ab03d`): `--provider gemini` targets Gemini's OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`); defaults: extraction `gemini-3-flash-preview`, embedding `gemini-embedding-001` 1536-dim, threshold 0.85. Auth via `GEMINI_API_KEY`/`GOOGLE_API_KEY`, auto-loaded from repo-root `.env` (gitignored) by a no-dependency loader.
- **Provider-aware request shaping** (`OpenAIChatClient`):
  - `max_completion_tokens`: gemini 16384, ollama 2048, `MEMBOX_EVAL_MAX_COMPLETION_TOKENS` overrides — gemini-3-flash-preview is a thinking model, the old hardcoded 2048 silently truncated 34/58 chunk extractions.
  - `reasoning_effort`: gemini default `"low"`, `MEMBOX_EVAL_REASONING_EFFORT` overrides, `none`/`""` disables — cuts per-chunk latency ~3× for structured extraction. Set in both `parse` and `create` paths.
- **`--max-files N` smoke mode**: ingests first N corpus files and evaluates only gold questions answerable from them. ~1/6 API cost for iteration; full runs reserved for milestones.
- **First Gemini full eval result — 53.8% (14/26)**, single_hop 7/15, multi_hop 4/7, temporal 3/4. Zero extraction failures after the token-cap fix. Mean output tokens 953.
- **Critical gap discovered (key finding)**: offline pure-FTS hits **80.8%** but Gemini full-pipeline (graph+FTS) hits only **53.8%**. Root cause: the FTS fallback is **either/or** — once graph recall returns ≥1 triple, FTS never fires and is invisible to `compact_output`. Gemini's decent-but-imperfect extraction produces triples that *look* relevant but often lack the answer keyword, and they crowd out the FTS evidence that would have answered. This is the real blocker to the 80% acceptance gate.

### Session 7 (2026-06-11) — Graph + FTS fusion
- **Step 0 BM25 scorer fix implemented**: `_bm25_scores_for_relations` now uses OR-of-tokens FTS5 matching (`_fts5_or_query`) instead of phrase matching (`_fts5_escape`). This restored the lexical component of graph triple scoring but did not change hit rate by itself: 53.8% overall, single 7/15, multi 4/7, temporal 3/4, mean 1033 tokens.
- **Step 1 budget-partitioned fusion implemented**: default `RetrievalConfig.fusion_mode="merge"` fetches both graph triples and direct FTS chunks, then renders them through a three-pass budget partition (`chunk_share=0.4`): triple pass, chunk pass, triple backfill. `fusion_mode="fallback"` preserves the old either/or behavior for A/B testing and rollback.
- **Acceptance run passed** (`chunk_share=0.4`, `fts_fallback_k=5`): 84.6% overall (22/26), single 12/15, multi 6/7, temporal 4/4, mean 1551 tokens. This cleared the Phase 7.5 retrieval gates: overall >=80%, temporal 100%, multi-hop not below 4/7, default 2000-token budget. The footer remains budget-exempt, so one observed 2006-token output is expected and not a budget-contract violation.
- **Final shipped default**: `fts_fallback_k=10` recovered q11 with no regression: 88.5% overall (23/26), single 13/15, multi 6/7, temporal 4/4, mean 1941 tokens. The prior 22 hits stayed hits. `chunk_share` sweep over 0.25 / 0.4 / 0.6 at k=5 produced identical hit sets, so `chunk_share=0.4` remains the robust default.
- Remaining shipped-baseline misses are q08, q12, and q19. q08 has the key graph triple but its answer-bearing source chunk ranks below the shipped FTS candidate cap; q19 admits m5go candidates with k=10 but loses the relevant keyword chunk during budget admission; both are default-2000-token budget/ranking tradeoffs. q12 is a CJK FTS/query-construction gap in `main`: the corpus contains `癸水七杀格`, but the unicode61 + whitespace-token query path returns 0 rows. The current feature branch implements an additive trigram sidecar and CJK excerpts; q12 passes on a copied eval DB, full 26-question eval is still pending.

### Session 8 (2026-06-11) — CJK trigram sidecar verified
- **Review of the trigram sidecar commit** found one design deviation: the guarded LIKE fallback fired when the sidecar table was missing or when trigram MATCH returned 0 rows (precision regression + table scan on every CJK miss). Fixed: LIKE now fires only when the query yields no 3-char trigram terms; sidecar-missing and zero-hit cases fall through to the old unicode61 path. 7 tests added (fallback dispatch, project_filter on both CJK paths, 64-term cap).
- **Full 26-question Gemini eval rerun** (re-ingested copy of gemini3 DB): first 23/26 with q12 still MISS — trigram recall surfaced the answer chunk but anchor-density reranking placed it 3rd and a redundant second excerpt window pushed its excerpt past budget admission.
- **Excerpt/rerank fixes (CJK path only)**: maximal-term scoring in `_cjk_content_score` (subsumed n-grams no longer double-count, so fragment-repeating distractor chunks stop outranking the answer chunk) + marginal-gain gate on extra excerpt windows (must add a new anchor term or carry ≥ half the best window's weight). Details in `docs/design/cjk-trigram-fts.md` § Measured results.
- **Final verified: 24/26 (92.3%)** — q12 HIT, all 23 baseline HITs preserved, temporal 4/4, multi-hop 6/7, q08/q19 remain the only (pre-existing) misses. q12 also verified on a fresh pre-re-ingest DB copy. 420 tests, ruff + strict mypy clean.

---

## Current state

- **main** (HEAD `f6c8a99`): Phases 1-7 + 7.5 M1-M3, M6, FTS fallback, online-eval pipeline, Step 0 BM25 scorer fix, Step 1 graph+FTS budget fusion, and the CJK trigram sidecar (migration v5, CJK query dispatch, CJK excerpts) are merged. Verified eval baseline 24/26. The `f6c8a99` "working-tree cleanup" merge commit also contains the `gemini-3.1-flash-lite` default swap and the lifecycle design as a review draft.
- **`feature/lifecycle-design-v2`** (HEAD `4bfb427`, ahead of main by 4 commits): the lifecycle design at v2.3 (accepted) plus the `requires_corpus` CI fixture. **Needs to be merged into main** so the design lands in the main tree and Phase B can start. After that merge, the test corpus CI fix becomes part of main's CI gate.
- **Working-tree state on `feature/lifecycle-design-v2`**: clean (session 9 committed everything).
- `feature/cjk-trigram-fts-design` is merged to main and can be deleted after user confirms (no longer the working branch).
- **Old phase 1-7 feature branches + `develop`** still exist but are historical; main is authoritative. Safe to delete after confirmation.
- **Working eval DBs**: `/tmp/membox-eval-gemini3.db` (58 chunks, 459 entities, 340 relations — cleanest Gemini run; basis for the 53.8% result). `/tmp/membox-eval-m3.db` (Ollama baseline, 51 chunks). `/tmp/membox-eval-gemini3-trigram-full.db` (re-ingested, v5 sidecar, basis for the 24/26 result).
- **`eval/corpus/` is gitignored** (private handoff docs from local projects). Tests in `tests/test_eval_corpus.py` that need it skip cleanly on CI via the new `requires_corpus` fixture; they only run on developer machines.

### Locked architectural decisions
- **Single global DB** (`~/.membox/membox.db` default; `--db` > `MEMBOX_DB` env > default). No per-project DBs, no registry, no ATTACH federation. `documents.project` column scopes; entities/relations are global for cross-project identity.
- **Provider defaults** (last calibrated 2026-06-10, the Lite swap is on main but pre-decision): extraction ollama `gemma-4-E2B:latest`, embedding ollama `qwen3-embedding:latest` 1024-dim, disambig threshold 0.72 (same-entity ≥0.763, diff-entity ≤0.680 measured). **Online alternative via `--provider gemini`**: extraction `gemini-3.1-flash-lite` with `reasoning_effort="low"` (this is the working-tree change made before session 8; session 8 evals used it and ran clean at 24/26), embedding `gemini-embedding-001` 1536-dim, disambig threshold 0.85. All overridable via `MEMBOX_EVAL_*` env vars.
- **Read path has no LLM**: pruning is ranking + budgeting only (spec §3.7). Compact budgeted output is the default; silent truncation/staleness forbidden (coverage footer).
- **Write path is async** (M6, implemented): enqueue in ms, transient worker materializes the graph; eventual consistency surfaced in query footer. No resident daemon. `--sync` exists for scripts/eval that need determinism.
- **Default retrieval is graph+FTS fusion**: `fusion_mode="merge"` is the default; `fusion_mode="fallback"` preserves the old either/or path for A/B and rollback.
- No external services / hosted vector DBs by default; SQLite WAL, FK ON, per-thread connections, RLock (unchanged).
- `eval/corpus/` is gitignored (frozen content + local-only data). Re-snapshotting is an M4-era task paired with updating temporal gold answers.
- **Lifecycle design (next stage, accepted on feature branch — promote on merge)**: Trace → Unit → Crystal, owner-confirmed at v2.3. Key invariants: closed unit-type taxonomy (8 types); closed 11-label set; dedup anchored on source identity not content; cross-process writes take a per-project `lifecycle_lease` (not the in-process RLock); decay owned by `memory consolidate --apply`; no Membox-managed blob storage ever (`payload_locator` + `history fetch` re-read the upstream session jsonl); `history`/`memory search` default to current project; lifecycle eval fixtures are the first deliverable of Phase C.

---

## Open questions / decisions needed

1. ~~CJK/trigram FTS full eval~~ — **resolved session 8**: 24/26, q12 HIT, no regression; merged to main.
2. **Merge `feature/lifecycle-design-v2` into main** — the design is accepted and the test corpus CI fix is needed in main's CI gate. Use `merge --no-ff` per project convention (see "New conventions"). After merge, Phase B implementation is unblocked.
3. **Confirm or revert the Lite default**: `scripts/eval_memory.py` is on main with `gemini-3.1-flash-lite` as the default Gemini extraction model. Origin pre-dates session 8; session 8 evals ran clean at 24/26 with it. Decide: keep (formalize in `scripts/eval_memory.py` docs), revert to `gemini-3-flash-preview`, or pin a different model.
4. **Ingest performance**: even with `reasoning_effort=low`, 58 chunks take about 35 min on Gemini because every entity/relation still triggers a single-text embed call (about 900 serial HTTPS round-trips). Three queued optimizations: process-internal embed cache, batched embed calls (`OpenAIEmbedClient.embed()` already takes `list[str]`), chunk-level concurrency. Expected 3-5x speedup.
5. **Residual recall misses**: q08 and q19 remain — both default-2000-token budget/ranking tradeoffs on the English path.
6. **Extraction quality on small local models**: 258/600 entities typed "Unknown"; some garbage whole-sentence entity names; 7 chunks still exceed 2048 completion tokens. Not pursued on Gemini path; revisit only if local Ollama becomes the priority again.
7. Old branches + `develop` cleanup — delete after user confirms.
8. Phase 8 (tree-sitter), Phase 9 (skill file), Phase 10 (release 0.2.0) — queued behind 7.5. Phase 9 depends on M6 (done).
9. **Lifecycle eval fixture design** (Phase C first deliverable): categories listed in `docs/design/agent-memory-lifecycle.md` Evaluation Strategy — explicit user rules, ephemeral chatter that should stay trace, plans becoming decisions, facts superseded by newer sources, repeated failures → learning/procedure, conflicting memories (surface not merge), user corrections retracting/superseding old units. Must be in place before Phase C's gate/activation thresholds can be tuned.

---

## Next concrete steps

1. **Merge `feature/lifecycle-design-v2` into main** (`merge --no-ff`, then a separate handoff commit per convention). The test corpus CI fix lands with it; the design lands as the active design surface for the next track.
2. **Decide on the Lite default** (keep, revert, or pin differently).
3. Ingest-performance work (embed cache, batched embedding calls, chunk-level concurrency) — start on a new `feature/*` branch. **Do not chain merge+test in one Bash call** (lesson from session 8).
4. **M4 supersession semantics** (schema migration: `relations.superseded_by`), then re-snapshot corpus + update temporal gold answers. Owner-direction on Lite default may affect which tests to commit with the migration.
5. **M5 close-the-loop**: `membox ingest-file docs/HANDOFF.md` end-to-end with the tuned retrieval.
6. **Phase B (history trace index)** can start immediately after step 1: `history_sessions/messages/events` tables (next migration after v5), `membox-history-jsonl` fixture format + importer protocol, secret-redaction scrubber, `history search/around/fetch/file/failures` commands, incremental re-import tests, CJK-safe FTS. Per the lifecycle design's Phase B acceptance criteria.
7. Cleanup: delete the stale `feature/*` (phase 1-7, phase 7.5 sub-branches including `feature/cjk-trigram-fts-design`) and `develop` branches after the user confirms.

---

## Notes / scratchpad

- Acceptance gates for retrieval are met; shipped defaults `chunk_share=0.4`, `fts_fallback_k=10`: 24/26 (92.3%) with CJK sidecar, temporal 100%, multi-hop 6/7.
- Reference points: Ollama 11.5% / offline pure-FTS 80.8% / Gemini either-or 53.8% / Step 0 only 53.8% / Step 1 fusion k=5 84.6% / fusion k=10 88.5% / + CJK sidecar 92.3%.
- Eval DBs: `/tmp/membox-eval-gemini3-trigram-full.db` (re-ingested, v5 sidecar, basis for the 24/26 result) supersedes gemini3 as the working DB; gemini3 retained as the pre-v5 original.
- Mean output tokens: 275 (Ollama baseline) → 1278 (offline FTS) → 953 (Gemini either-or) → 1033 (Step 0) → 1551 (Step 1 fusion k=5) → 1941 (shipped fusion k=10).
- ResourceWarning re unclosed SQLite connections in CLI tests — cosmetic, future `close()` cleanup.
- The "why did graph+FTS fall below pure-FTS" finding is resolved by fusion: graph hits no longer suppress FTS evidence.
