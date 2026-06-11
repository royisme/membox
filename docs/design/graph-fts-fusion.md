# Graph + FTS Fusion Retrieval Design

Status: implemented and accepted
Date: 2026-06-11
Scope: Phase 7.5 memory-quality gate, handoff open question #1

## Summary

Membox now uses budget-partitioned graph + FTS fusion as the default query path.
The previous implementation treated FTS as an either/or fallback: once graph
retrieval returned at least one scored triple, direct FTS chunks were not shown.
That suppressed keyword-bearing source evidence and blocked the Phase 7.5
quality gate.

The accepted design keeps graph triples and FTS chunks in separate ranking
pools. Triples keep the existing graph scoring formula. Chunks keep the FTS5
BM25 order. Fusion happens only at the token-budget layer through `chunk_share`,
so the implementation avoids cross-type score calibration and avoids expensive
chunks crowding out all cheap graph triples.

## Results

The default `chunk_share=0.4` passed the acceptance gate on the Gemini eval DB:

| Configuration | Overall | Single-hop | Multi-hop | Temporal | Mean tokens |
|---|---:|---:|---:|---:|---:|
| Baseline either/or fallback | 53.8% | 7/15 | 4/7 | 3/4 | 953 |
| Step 0 only, BM25 query fix | 53.8% | 7/15 | 4/7 | 3/4 | 1033 |
| Step 1 fusion, `chunk_share=0.4`, k=5 | 84.6% | 12/15 | 6/7 | 4/4 | 1551 |
| Step 1 fusion + `fts_fallback_k=10` (shipped defaults) | 88.5% | 13/15 | 6/7 | 4/4 | 1941 |

The `chunk_share` sweep (0.25 / 0.4 / 0.6 at k=5) produced identical hit rates
and identical misses across all three values, confirming 0.4 is robust: leftover
flow between passes makes the partition insensitive on this corpus, so
`chunk_share` acts as a floor guarantee rather than a sensitive tuning knob.

Acceptance requirements:

- Overall hit rate >= 80%.
- Temporal hit rate = 100%.
- Multi-hop must not regress below 4/7.
- Default budget remains 2000 tokens.

All requirements are met. The multi-hop score improved from 4/7 to 6/7, so the
budget partition did not create the expected reverse-crowding risk.

## Step 0: BM25 Query Fix

The relation BM25 scorer used phrase matching via `_fts5_escape(query)`. Full
natural-language questions almost never match evidence chunks as exact phrases,
so `bm25(t)` was effectively zero for many graph triples.

Step 0 changed `_bm25_scores_for_relations` to use `_fts5_or_query(query)`, the
same OR-of-tokens query builder already used by the direct FTS fallback. This
fix restores the lexical component of the triple score while preserving the
existing min-max normalisation over the triple candidate set.

Step 0 did not change hit rate by itself. That result is useful: it shows the
core miss mode was not triple ordering, but missing answer text in the graph
triple pool.

## Step 1: Budget-Partitioned Fusion

The default `fusion_mode="merge"` path produces two candidate pools:

- Triple pool: `scored_query(...)`, sorted by the existing composite graph score.
- Chunk pool: `fts_fallback_chunks(...)`, sorted by SQLite FTS5 BM25.

No score is compared across pools.

With `budget=2000` and `chunk_share=0.4`, the renderer computes:

```text
chunk_reserve = floor(budget * chunk_share)
triple_allowance = budget - chunk_reserve
```

The output renderer then runs three passes:

1. Triple pass: admit graph triple lines and top-K graph evidence within
   `triple_allowance`.
2. Chunk pass: admit direct FTS chunks within `chunk_reserve + pass1_leftover`.
   A chunk is skipped if the same `doc_id` was already emitted as graph evidence.
3. Triple backfill pass: if chunk admission leaves unused budget, admit more
   graph triple lines without retrying evidence.

The footer reports both coverage dimensions:

```text
(returned N/M triples, K/L FTS chunks, ~X/Y tokens; raise --budget for more)
```

The footer itself is not charged to the budget, so full output token counts may
appear slightly above the caller budget. This is expected and consistent with
the existing compact-output contract.

## Why This Design

Rejected alternatives:

- Unified score merge: would require a new `chunk_weight` to compare graph
  triples against chunks, and would make graph scores drift when chunk candidates
  change.
- Graph-then-fill thresholding: the failure mode was graph confidence looking
  plausible while missing the answer keyword, so threshold gating had no stable
  signal.
- RRF: useful as an experiment, but still produces a single cross-type list and
  keeps the token-crowding problem.

Budget partitioning keeps the only new parameter in token-budget units. That
makes tuning and failure attribution straightforward.

## Compatibility

`fusion_mode="fallback"` keeps the original either/or control flow for A/B
comparison and rollback:

- No resolved graph seeds: direct FTS fallback.
- No scored graph triples: direct FTS fallback.
- Non-empty graph triples: graph-only compact output.

`fts_fallback_k=0` disables the FTS chunk channel. In merge mode this degrades
to graph-only output; in fallback mode it preserves the old bare-footer behavior
when no graph evidence exists.

## Remaining Misses (per-question root cause, verified against the eval DB)

All four original misses had their answer keywords present in the corpus — none
was a gold/corpus gap. Verified by replaying the exact FTS MATCH expressions and
keyword queries against `/tmp/membox-eval-gemini3.db`:

| Q | Root cause | Status |
|---|---|---|
| q11 | Answer doc ranked #6 in FTS; cut off by `fts_fallback_k=5` | **Fixed** by k=10 (now HIT) |
| q19 | k=10 admits the first m5go candidates that were cut off at k=5, but the relevant keyword chunk still loses during budget admission | Remaining miss; budget pressure at the default 2000-token budget |
| q08 | Graph contains the key `handleAnswerNow -> handleWhatToSay` triple, but the answer-bearing source chunk ranks below the shipped FTS candidate cap; `chunk_share` tuning did not recover it | Remaining miss; ranking + budget tradeoff under the default 2000-token budget |
| q12 | FTS5 `unicode61` does not segment CJK well enough for whitespace-free Chinese questions, so the current MATCH expression returns 0 rows even though the corpus contains `癸水七杀格`. Graph side also lacks the needed CJK entities | Addressed on `feature/cjk-trigram-fts-design` with a trigram sidecar and CJK excerpts; full eval rerun pending |

## Follow-up Work

- **CJK FTS full eval**: the follow-up branch implements an additive
  `documents_fts_trigram` sidecar and CJK query-time excerpts. q12 passes on a
  copy of `/tmp/membox-eval-gemini3.db`; the remaining work is to rerun the full
  26-question eval and confirm no regression from the shipped 23/26 baseline.
- Consider shared FTS query execution only after preserving equivalence with the
  current two-query implementation.
- Move to M4 supersession semantics now that the Phase 7.5 retrieval gate is met.
