# Membox — Session Handoff

> Single source of truth for cross-session context. Read at session start; update before ending.

**Last updated**: 2026-06-11 (session 8 — CJK trigram sidecar verified and merged)
**Current phase**: Phase 7.5 retrieval quality gate complete. M1-M3, M6, retrieval fallback, online eval, graph+FTS fusion, and the CJK trigram sidecar are implemented and merged. **Verified retrieval baseline is 24/26 (92.3%)** — q12 (CJK) now HIT; remaining misses q08/q19 are known budget/ranking tradeoffs. Temporal 100%, multi-hop 6/7, default 2000-token budget contract.

### Pending user decisions before next work item
- `scripts/eval_memory.py` has an **unstaged working-tree edit** changing the default Gemini extraction model `gemini-3-flash-preview` → `gemini-3.1-flash-lite`. Origin unclear (made before session 8). Session 8 evals used it and ran clean (24/26, 1 transient 503). **Commit, revert, or pin differently?**
- `docs/design/agent-memory-lifecycle.md` is an **untracked review draft** (Trace→Unit→Crystal next-stage design). Not yet reviewed; defer until M4/M5 progress so the design lands with context.
- Stale `feature/*` branches and `develop` — safe to delete after user confirms (see "Next concrete steps").

### New conventions worth remembering across sessions
- **Merge into main uses `merge --no-ff` with a `merge: <theme>` message**, then a separate commit for handoff/design-doc syncs on the feature branch. See `git log main --merges` for examples. No PR/CODEOWNERS flow is required.
- **When asking a subagent to "merge and run tests", do NOT chain them in one Bash call with `run_in_background: true`** — the output buffer hides pytest results and forces poll loops. Pattern: merge first (seconds), then a separate `pytest` call. Lesson learned in session 8.
- **Long-running commands that print only at the end** (full eval, ingest) can be run in the background, but a "wait for terminal output" pattern via `until grep ...; do sleep N; done` blocks the shell and is rejected by the harness. Use `run_in_background: true` and let the harness notify on exit.

---

## What's been done

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

- **main**: Phases 1-7 + 7.5 M1-M3, M6, FTS fallback, online-eval pipeline, Step 0 BM25 scorer fix, Step 1 graph+FTS budget fusion, and the CJK trigram sidecar (migration v5, CJK query dispatch, CJK excerpts) are merged. Verified eval baseline 24/26. HEAD = `77c4090`; origin and local are in sync.
- **Working-tree state on main**:
  - `scripts/eval_memory.py` modified (default extraction model) — uncommitted, awaiting user decision.
  - `docs/design/agent-memory-lifecycle.md` untracked — awaiting review.
- `feature/cjk-trigram-fts-design` is now merged and can be deleted after user confirms (no longer the working branch).
- **Old phase 1-7 feature branches + `develop`** still exist but are historical; main is authoritative. Safe to delete after confirmation.
- **Working eval DBs**: `/tmp/membox-eval-gemini3.db` (58 chunks, 459 entities, 340 relations — cleanest Gemini run; basis for the 53.8% result). `/tmp/membox-eval-m3.db` (Ollama baseline, 51 chunks).

### Locked architectural decisions
- **Single global DB** (`~/.membox/membox.db` default; `--db` > `MEMBOX_DB` env > default). No per-project DBs, no registry, no ATTACH federation. `documents.project` column scopes; entities/relations are global for cross-project identity.
- **Provider defaults** (last calibrated 2026-06-10): extraction ollama `gemma-4-E2B:latest`, embedding ollama `qwen3-embedding:latest` 1024-dim, disambig threshold 0.72 (same-entity ≥0.763, diff-entity ≤0.680 measured). **Online alternative via `--provider gemini`**: extraction `gemini-3-flash-preview` with `reasoning_effort="low"` (working-tree edit suggests switching default to `gemini-3.1-flash-lite` — uncommitted, see "Pending user decisions"), embedding `gemini-embedding-001` 1536-dim, disambig threshold 0.85. All overridable via `MEMBOX_EVAL_*` env vars.
- **Read path has no LLM**: pruning is ranking + budgeting only (spec §3.7). Compact budgeted output is the default; silent truncation/staleness forbidden (coverage footer).
- **Write path is async** (M6, implemented): enqueue in ms, transient worker materializes the graph; eventual consistency surfaced in query footer. No resident daemon. `--sync` exists for scripts/eval that need determinism.
- **Default retrieval is graph+FTS fusion**: `fusion_mode="merge"` is the default; `fusion_mode="fallback"` preserves the old either/or path for A/B and rollback.
- No external services / hosted vector DBs by default; SQLite WAL, FK ON, per-thread connections, RLock (unchanged).
- `eval/corpus/` is frozen: gold answers (incl. temporal q23) depend on the snapshotted content. Re-snapshotting is an M4-era task paired with updating temporal gold answers.

---

## Open questions / decisions needed

1. ~~CJK/trigram FTS full eval~~ — **resolved session 8**: 24/26, q12 HIT, no regression; merged to main.
2. **Ingest performance**: even with `reasoning_effort=low`, 58 chunks take about 35 min on Gemini because every entity/relation still triggers a single-text embed call (about 900 serial HTTPS round-trips). Three queued optimizations: process-internal embed cache, batched embed calls (`OpenAIEmbedClient.embed()` already takes `list[str]`), chunk-level concurrency. Expected 3-5x speedup.
3. **Residual recall misses**: q08 and q19 remain — both default-2000-token budget/ranking tradeoffs on the English path.
3a. **Uncommitted working-tree items needing a user decision**: (a) `scripts/eval_memory.py` has a local edit switching the default Gemini extraction model `gemini-3-flash-preview` → `gemini-3.1-flash-lite` (origin unclear — possibly made outside a session; all session-8 eval runs used it). Commit or revert? (b) `docs/design/agent-memory-lifecycle.md` review draft — commit when its review starts.
4. **Extraction quality on small local models**: 258/600 entities typed "Unknown"; some garbage whole-sentence entity names; 7 chunks still exceed 2048 completion tokens. Not pursued on Gemini path; revisit only if local Ollama becomes the priority again.
5. Old branches + `develop` cleanup — delete after user confirms.
6. Phase 8 (tree-sitter), Phase 9 (skill file), Phase 10 (release 0.2.0) — queued behind 7.5. Phase 9 depends on M6 (done).

---

## Next concrete steps

1. **Decide on the two pending items first** (extraction-model default; lifecycle design draft). Then:
2. Ingest-performance work (embed cache, batched embedding calls, chunk-level concurrency) — start on a new `feature/*` branch. **Do not chain merge+test in one Bash call** (lesson from session 8).
3. M4 supersession semantics (schema migration: `relations.superseded_by`), then re-snapshot corpus + update temporal gold answers.
4. M5 close-the-loop: `membox ingest-file docs/HANDOFF.md` end-to-end with the tuned retrieval.
5. Cleanup: delete the stale `feature/*` (phase 1-7, phase 7.5 sub-branches) and `develop` branches after the user confirms.

---

## Notes / scratchpad

- Acceptance gates for retrieval are met; shipped defaults `chunk_share=0.4`, `fts_fallback_k=10`: 24/26 (92.3%) with CJK sidecar, temporal 100%, multi-hop 6/7.
- Reference points: Ollama 11.5% / offline pure-FTS 80.8% / Gemini either-or 53.8% / Step 0 only 53.8% / Step 1 fusion k=5 84.6% / fusion k=10 88.5% / + CJK sidecar 92.3%.
- Eval DBs: `/tmp/membox-eval-gemini3-trigram-full.db` (re-ingested, v5 sidecar, basis for the 24/26 result) supersedes gemini3 as the working DB; gemini3 retained as the pre-v5 original.
- Mean output tokens: 275 (Ollama baseline) → 1278 (offline FTS) → 953 (Gemini either-or) → 1033 (Step 0) → 1551 (Step 1 fusion k=5) → 1941 (shipped fusion k=10).
- ResourceWarning re unclosed SQLite connections in CLI tests — cosmetic, future `close()` cleanup.
- The "why did graph+FTS fall below pure-FTS" finding is resolved by fusion: graph hits no longer suppress FTS evidence.
