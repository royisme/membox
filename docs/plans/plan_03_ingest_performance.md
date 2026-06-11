# Plan 03 — Ingest Performance: Embed Cache + Batched Embeds + Chunk Concurrency

> **Status**: Ready for execution · **Context**: HANDOFF open question #4 / next step #3. Expected 3–5× ingest speedup.
> **Problem**: 58 corpus chunks take ~35 min on Gemini even with `reasoning_effort=low`, because every entity name and relation triple triggers a single-text embed call — ~900 serial HTTPS round-trips per full ingest.

## Where the round-trips come from (measured call chain)

- `Embedder` Protocol is single-text: `embed(text) -> list[float]` (`services/embedding.py:20`). `EmbeddingService.embed` wraps each text in a 1-element list → `OpenAIEmbedClient.embed([text])[0]` (`embedding.py:74`) — one HTTPS round-trip per call.
- **`OpenAIEmbedClient.embed(texts: list[str])` already accepts batches** (`providers/openai_compat.py:118`) — the wire layer needs no change.
- Per chunk in `MemoryAgent.ingest_extracted` (`core/agent.py:59`): every entity → `find_or_create_entity` which embeds the name for disambiguation (`agent.py:96, 104, 109` → `entities.py:224`); every relation → one embed of the triple text (`agent.py:115–122`). ≈ N + 2M embeds per chunk, many of them the same entity name repeated (an entity appears as subject/object of several relations).
- No caching exists anywhere today.

## Milestones (independent, in order of payoff/risk; each is its own branch + merge)

### P1 — Process-internal embed cache (low risk, immediate win)

In-memory dict keyed `(model, text)` → vector, wrapping `EmbeddingService` (or injected into it). Scope: lifetime of the `MemoryAgent`/process — no persistence, no invalidation problem. This alone removes the repeated-entity-name calls (the dominant duplication within one document: subject/object names recur across relations and chunks).

Consistency note: the cache sits on the embed step itself, so disambiguation (`find_similar_entity`, threshold 0.85 / 0.70 for embeddinggemma) sees identical vectors for identical text — strictly an improvement (eliminates any provider-side nondeterminism between duplicate calls). Cap size (e.g. simple bounded dict, default ~10k entries) to bound memory in long-lived processes; knob in `EmbeddingConfig`.

Tests: cache hit avoids client call (count calls on the fake embedder), cap eviction, model-key separation.

### P2 — Batched embed calls (medium risk: touches the ingest loop shape)

Restructure `ingest_extracted` to collect embed-needing texts per chunk and issue them in one (or few) `OpenAIEmbedClient.embed(list)` calls:

- Pass 1: collect unique entity names from `graph.entities` + relation endpoints, and relation triple texts.
- One batched embed (minus cache hits), populate the cache.
- Pass 2: run the existing find-or-create/upsert logic, now hitting only the cache.

This keeps `find_or_create_entity`'s signature and the disambiguation cascade untouched — the storage layer still calls `embedder.embed(name)`, it just resolves from cache. Add `embed_batch_size` to `EmbeddingConfig` (provider limits: Gemini and OpenAI accept large batches; Ollama handles lists too) and chunk the batch accordingly.

Caveat to verify in implementation: `OpenAIEmbedClient.embed` sends a scalar for len-1 lists — confirm response parsing handles both shapes for every supported provider (gemini, ollama). Eval gate: `--offline` run produces identical results pre/post (the SemanticDummyEmbedder is deterministic, so vectors must be byte-identical).

### P3 — Chunk-level concurrency (highest risk; do last, separately)

Parallelize the per-chunk pipeline (extraction LLM call + batched embed) across a small thread pool, serializing only the DB write section:

- Extraction + embedding are pure I/O-bound HTTPS — safe to parallelize.
- DB writes stay serialized: per-thread connections exist (`ConnectionManager`, `threading.local`), but the find-or-create critical section requires the `write_lock` RLock, and cross-chunk entity dedup is order-sensitive. Simplest safe shape: workers produce `(graph, embeddings)` results into a queue; the main thread materializes them in order. This preserves today's semantics exactly (same dedup outcomes regardless of worker timing).
- Knob: `ingest_concurrency` (default 1 = today's behavior; eval/CLI can raise it). Natural home: a new small `IngestConfig` on `MemboxConfig`, or `ExtractionConfig` — decide at implementation; do NOT bury it in `RetrievalConfig`.
- The async queue worker (`core/worker.py drain_queue`) stays single-process/single-lease; concurrency lives INSIDE one `ingest_content` call, so the worker and `--sync` paths both benefit without touching the lease protocol.

Tests: deterministic-order materialization under a fake slow extractor, no `IntegrityError` leaks under concurrent find-or-create (existing phase-6 concurrency tests must stay green), `ingest_concurrency=1` is the default and bit-identical to today.

## Acceptance

- Full corpus ingest (58 chunks, Gemini) measured before/after each milestone; target ≥3× cumulative speedup after P2+P3. Record timings in the PR descriptions and HANDOFF.
- `scripts/eval_memory.py --offline` results unchanged at every milestone (determinism gate).
- One full Gemini eval after all three land: hit set must equal the current baseline (24/26, or the plan_02 re-baseline if that landed first — coordinate; **prefer landing plan_03 P1+P2 before plan_02's full rerun** so the rerun is cheap).
- Standard gates: pytest (incl. phase-6 concurrency suite) + ruff + strict mypy green, coverage ≥ 80%.

## Constraints for dispatched subagents

- Branches: `feature/ingest-embed-cache`, `feature/ingest-embed-batching`, `feature/ingest-concurrency`. One milestone per branch; merge with `--no-ff` per convention. Do not chain merge+pytest in one backgrounded Bash call.
- No new dependencies (stdlib `threading`/`concurrent.futures` only for P3).
- `Embedder` Protocol changes, if any, must keep the fake embedders in tests working; mock only at the HTTP boundary.
- Escalate instead of guessing on: provider batch-size limits, any observed nondeterminism in offline eval, or if P3's serialize-writes design proves insufficient for the target speedup.
