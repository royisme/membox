"""BFS multi-hop graph retrieval with hybrid scoring, token-budget truncation, and compact output.

Implements the composite scoring formula from spec §3.7::

    score(t) = decay^hops(t) * ( a*sim(t) + (1-a)*bm25(t) )

Where:
- ``hops(t) = min(depth(subject), depth(object))``
- ``sim(t)`` is (1 + cosine) / 2 between the stored relation embedding and the
  query embedding (0 when no embedder is configured)
- ``bm25(t)`` is the maximum negated-then-min-max-normalised FTS5 BM25 score
  over the relation's evidence documents (0 if no FTS match)
- ``a`` (alpha) is redistributed to BM25 when no embedder is configured

Token budget is estimated via the deterministic formula::

    est_tokens(s) = CJK_char_count(s) + ceil(non_CJK_char_count(s) / 4)

Greedy best-effort knapsack: items sorted descending by score; each item is
admitted if its cost fits the remaining budget, skipped otherwise (not stopped).
An evidence snippet is only eligible if its parent triple was already admitted.

The coverage footer (``(returned N/M triples, ~X/Y tokens; ...)`` is always
appended and its ~20-token cost is excluded from the budget calculation.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import TYPE_CHECKING, cast

# est_tokens is defined in core.tokens and re-exported here for backward
# compatibility: callers that import it from this module continue to work.
from membox.core.store.fts import (
    CJK_ANCHOR_LIMIT,
    CJK_TRIGRAM_LIMIT,
    cjk_anchor_terms,
    cjk_trigram_terms,
    contains_cjk,
    fts5_or_query,
    fts5_query_from_terms,
    fts_table_exists,
)
from membox.core.tokens import est_tokens as est_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable

    from membox.config import RetrievalConfig
    from membox.model.schema import HopResult

# BFS depth for seed entities.
_SEED_DEPTH = 0
_CJK_TRIGRAM_LIMIT = CJK_TRIGRAM_LIMIT
_CJK_ANCHOR_LIMIT = CJK_ANCHOR_LIMIT
_cjk_anchor_terms = cjk_anchor_terms
_cjk_trigram_terms = cjk_trigram_terms
_contains_cjk = contains_cjk
_fts5_or_query = fts5_or_query
_fts5_query_from_terms = fts5_query_from_terms
_fts_table_exists = fts_table_exists
_CJK_EXCERPT_BEFORE_CHARS = 80
_CJK_EXCERPT_AFTER_CHARS = 260
_CJK_EXCERPT_WINDOWS = 2
_LEXICAL_EXCERPT_THRESHOLD_TOKENS = 700
_LEXICAL_EXCERPT_BEFORE_CHARS = 320
_LEXICAL_EXCERPT_AFTER_CHARS = 680
_LEXICAL_EXCERPT_WINDOWS = 2
_LEXICAL_STOPWORDS = {
    "about",
    "after",
    "are",
    "and",
    "core",
    "does",
    "for",
    "have",
    "into",
    "its",
    "main",
    "part",
    "projects",
    "project",
    "their",
    "the",
    "use",
    "uses",
    "using",
    "what",
    "which",
    "with",
}


class RetrievalOps:
    """BFS retrieval operations, mixed into :class:`KnowledgeStore`.

    Relies on the entity and relation mixins for ``get_entity``,
    ``get_neighbors``, ``get_evidence_docs``, and ``get_evidence_docs_with_meta``.
    """

    # Provided by sibling mixins (declared for type checking).
    if TYPE_CHECKING:

        def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None: ...

        def get_neighbors(
            self,
            entity_ids: Iterable[int],
            *,
            include_superseded: bool = False,
        ) -> list[tuple[int, int, int, str]]: ...

        def get_evidence_docs(self, relation_ids: Iterable[int]) -> list[tuple[int, int, str]]: ...

        def get_evidence_docs_with_meta(
            self, relation_ids: Iterable[int]
        ) -> list[tuple[int, int, str, str | None, str | None, str | None, str | None]]: ...

        def get_relation_embedding(self, relation_id: int) -> list[float] | None: ...

    def bfs_query(
        self,
        seed_ids: list[int],
        max_hops: int,
        *,
        include_superseded: bool = False,
    ) -> HopResult:
        """BFS from seed_ids for up to max_hops. Returns traversal result with lineage.

        Args:
            seed_ids: Starting entity ids.
            max_hops: Maximum number of BFS expansions.
            include_superseded: When True, superseded relations are included in
                the traversal.  Defaults to False (active relations only).

        Returns:
            HopResult with triplets, documents, and visited entities.
        """
        from membox.model.schema import HopResult as HopResultModel

        visited: set[int] = set(seed_ids)
        frontier: set[int] = set(seed_ids)
        # relation_id → (rid, source_id, target_id, predicate)
        collected: dict[int, tuple[int, int, int, str]] = {}

        for _ in range(max_hops):
            if not frontier:
                break
            edges = self.get_neighbors(frontier, include_superseded=include_superseded)
            new_frontier: set[int] = set()
            for rid, src, tgt, pred in edges:
                collected[rid] = (rid, src, tgt, pred)
                for neighbor in (src, tgt):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        new_frontier.add(neighbor)
            frontier = new_frontier

        # Resolve entity ids → canonical names (cached)
        name_cache: dict[int, str] = {}

        def _name(eid: int) -> str:
            if eid not in name_cache:
                row = self.get_entity(eid)
                name_cache[eid] = row[1] if row else f"<{eid}>"
            return name_cache[eid]

        triplets = [(_name(s), p, _name(t)) for (_, s, t, p) in collected.values()]

        # Gather evidence documents, dedup by doc_id, preserve insertion order
        evidence = self.get_evidence_docs(list(collected.keys()))
        seen_docs: set[int] = set()
        docs: list[str] = []
        for _, did, content in evidence:
            if did not in seen_docs:
                seen_docs.add(did)
                docs.append(content)

        return HopResultModel(
            triplets=triplets,
            documents=docs,
            seed_names=[],
            visited_entities=[_name(e) for e in visited],
        )

    def bfs_query_with_depths(
        self,
        seed_ids: list[int],
        max_hops: int,
        *,
        include_superseded: bool = False,
    ) -> tuple[
        dict[int, tuple[int, int, int, str]],
        dict[int, int],
        dict[int, str],
    ]:
        """BFS from seed_ids preserving per-entity BFS depth for hop scoring.

        Args:
            seed_ids: Starting entity ids.
            max_hops: Maximum number of BFS expansions.
            include_superseded: When True, superseded relations are included in
                the traversal.  Defaults to False (active relations only).

        Returns:
            Tuple of:
            - ``collected``: ``{relation_id: (rid, source_id, target_id, predicate)}``
            - ``entity_depth``: ``{entity_id: BFS depth from seed (0 = seed)}``
            - ``name_cache``: ``{entity_id: canonical_name}``
        """
        visited: set[int] = set(seed_ids)
        frontier: set[int] = set(seed_ids)
        entity_depth: dict[int, int] = dict.fromkeys(seed_ids, _SEED_DEPTH)
        collected: dict[int, tuple[int, int, int, str]] = {}

        for depth in range(max_hops):
            if not frontier:
                break
            edges = self.get_neighbors(frontier, include_superseded=include_superseded)
            new_frontier: set[int] = set()
            for rid, src, tgt, pred in edges:
                collected[rid] = (rid, src, tgt, pred)
                for neighbor in (src, tgt):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        entity_depth[neighbor] = depth + 1
                        new_frontier.add(neighbor)
            frontier = new_frontier

        # Build name cache.
        name_cache: dict[int, str] = {}
        for eid in visited:
            row = self.get_entity(eid)
            name_cache[eid] = row[1] if row else f"<{eid}>"

        return collected, entity_depth, name_cache

    def _bm25_scores_for_relations(
        self,
        relation_ids: list[int],
        query: str,
    ) -> dict[int, float]:
        """Return min-max-normalised BM25 scores per relation_id.

        Queries ``documents_fts`` via the FTS5 ``bm25()`` function (which
        returns lower-is-better negative values); raw scores are negated
        before normalisation so higher is better.

        For relations with no FTS-matching evidence the score is 0.0.  When all
        candidate raw scores are identical (degenerate denominator), every score
        is 0.0.

        Args:
            relation_ids: Relation ids to score.
            query: Raw query string for FTS5 BM25 scoring.

        Returns:
            ``{relation_id: normalised_bm25}`` in ``[0.0, 1.0]``.
        """
        if not relation_ids or not query.strip():
            return {}

        conn = self._cm.connection()  # type: ignore[attr-defined]

        table_name, match_expr = _fts_table_and_query(conn, query)
        if table_name is None or match_expr is None:
            return {}

        placeholders = ",".join("?" * len(relation_ids))

        try:
            rows = conn.execute(
                f"SELECT re.relation_id, -bm25({table_name}) AS score "  # noqa: S608
                f"FROM {table_name} "
                f"JOIN relation_evidence re ON re.doc_id = {table_name}.rowid "
                f"WHERE {table_name} MATCH ? "
                f"  AND re.relation_id IN ({placeholders})",
                [match_expr, *relation_ids],
            ).fetchall()
        except Exception:
            return {}

        # Max raw score per relation_id (after negation, higher is better).
        best: dict[int, float] = {}
        for rid_row, score_row in rows:
            rid_int = int(rid_row)
            score_f = float(score_row)
            if rid_int not in best or score_f > best[rid_int]:
                best[rid_int] = score_f

        if not best:
            return {}

        # Min-max normalise within the candidate set.
        min_v = min(best.values())
        max_v = max(best.values())
        denom = max_v - min_v
        if denom == 0.0:
            # Degenerate case: all candidates have the same raw score → 0 for all.
            return dict.fromkeys(best, 0.0)

        return {rid: (v - min_v) / denom for rid, v in best.items()}

    def scored_query(
        self,
        seed_ids: list[int],
        max_hops: int,
        query: str,
        query_embedding: list[float] | None,
        config: RetrievalConfig | None = None,
        project_filter: str | None = None,
        *,
        include_superseded: bool = False,
    ) -> list[dict[str, object]]:
        """BFS + composite scoring per spec §3.7.  Returns sorted scored-triple dicts.

        Each dict contains:

        - ``relation_id`` (int)
        - ``subject`` (str)
        - ``predicate`` (str)
        - ``object`` (str)
        - ``hops`` (int): ``min(depth(subject), depth(object))``
        - ``score`` (float)
        - ``sim`` (float): normalised cosine component
        - ``bm25`` (float): normalised BM25 component
        - ``evidence``: list of ``(doc_id, content, project, source_path,
          section, doc_date)`` for this relation

        Args:
            seed_ids: BFS seed entity ids.
            max_hops: Maximum BFS hops.
            query: Original query string (used for BM25 scoring).
            query_embedding: Query vector for sim(t); None disables sim.
            config: :class:`~membox.config.RetrievalConfig`; uses spec defaults if None.
            project_filter: If set, restrict evidence to this project name.
            include_superseded: When True, superseded relations are included in
                the BFS traversal and scoring.  Defaults to False.

        Returns:
            List of scored dicts sorted descending by score with deterministic
            tie-breaking (hops asc, then newest doc_date desc).
        """
        from membox.config import RetrievalConfig
        from membox.core.store.vectors import cosine

        cfg = config or RetrievalConfig()
        hop_decay = cfg.hop_decay
        alpha = cfg.alpha

        # 1. BFS with depth tracking.
        collected, entity_depth, name_cache = self.bfs_query_with_depths(
            seed_ids, max_hops, include_superseded=include_superseded
        )
        if not collected:
            return []

        relation_ids = list(collected.keys())

        # 2. BM25 scores.
        bm25_map = self._bm25_scores_for_relations(relation_ids, query)

        # 3. Evidence with metadata.
        # Type: {rid: [(doc_id, content, project, source_path, section, doc_date)]}
        evidence_map: dict[
            int, list[tuple[int, str, str | None, str | None, str | None, str | None]]
        ] = {}
        for (
            rid_ev,
            doc_id,
            content,
            proj,
            sp,
            section,
            doc_date,
        ) in self.get_evidence_docs_with_meta(relation_ids):
            if project_filter is not None and proj != project_filter:
                continue
            evidence_map.setdefault(rid_ev, []).append(
                (doc_id, content, proj, sp, section, doc_date)
            )

        # When filtering by project, skip relations with no matching evidence.
        active_ids = (
            relation_ids
            if project_filter is None
            else [rid for rid in relation_ids if rid in evidence_map]
        )

        if not active_ids:
            return []

        # 4. Build scored list.
        results: list[dict[str, object]] = []
        eff_alpha = alpha if query_embedding is not None else 0.0

        for rid in active_ids:
            _, src_id, tgt_id, pred = collected[rid]
            subj = name_cache.get(src_id, f"<{src_id}>")
            obj = name_cache.get(tgt_id, f"<{tgt_id}>")

            d_subj = entity_depth.get(src_id, max_hops)
            d_obj = entity_depth.get(tgt_id, max_hops)
            hops = min(d_subj, d_obj)

            # sim(t) — cosine between stored relation embedding and query embedding.
            sim_val = 0.0
            if query_embedding is not None:
                rel_blob = self.get_relation_embedding(rid)
                if rel_blob is not None and len(rel_blob) == len(query_embedding):
                    try:
                        cos = cosine(query_embedding, rel_blob)
                    except ValueError:
                        cos = 0.0
                    sim_val = (1.0 + cos) / 2.0

            bm25_val = bm25_map.get(rid, 0.0)
            score = (hop_decay**hops) * (eff_alpha * sim_val + (1.0 - eff_alpha) * bm25_val)

            evs = evidence_map.get(rid, [])
            best_date = _best_doc_date(evs)

            results.append(
                {
                    "relation_id": rid,
                    "subject": subj,
                    "predicate": pred,
                    "object": obj,
                    "hops": hops,
                    "score": score,
                    "sim": sim_val,
                    "bm25": bm25_val,
                    "evidence": evs,
                    "_best_date": best_date,
                }
            )

        # 5. Sort: score desc → hops asc → newest date first.
        results.sort(
            key=lambda r: (
                -float(r["score"]),  # type: ignore[arg-type]
                int(r["hops"]),  # type: ignore[call-overload]
                _date_sort_key(str(r["_best_date"])),
            )
        )

        return results

    def compact_output(
        self,
        scored: list[dict[str, object]],
        budget: int,
        top_evidence_k: int = 3,
    ) -> str:
        """Build compact subject-grouped output with token-budget truncation.

        Applies a greedy best-effort knapsack: items sorted by score descending
        (already done by :meth:`scored_query`).  Each item is admitted if its
        estimated token cost fits the remaining budget; items that exceed the
        budget are *skipped* (not stopping the loop — best-effort fill).
        Evidence snippets are only eligible for the top-*K* scored triples,
        and only if their parent triple was admitted.

        The coverage footer (~20 tokens) is always appended and excluded from
        the budget calculation.

        Args:
            scored: Sorted scored-triple dicts from :meth:`scored_query`.
            budget: Token budget for triple lines and evidence snippets.
            top_evidence_k: Max triples eligible for evidence snippets.

        Returns:
            Multi-line compact string with triple groups, evidence, and footer.
        """
        total = len(scored)
        remaining = budget
        admitted_rids: list[int] = []

        # ---- Triple admission pass ----------------------------------------
        for item in scored:
            rid = int(item["relation_id"])  # type: ignore[call-overload]
            subj = str(item["subject"])
            pred = str(item["predicate"])
            obj = str(item["object"])
            line = f"{subj}: {pred} {obj}"
            cost = est_tokens(line)
            if cost <= remaining:
                admitted_rids.append(rid)
                remaining -= cost

        admitted_set = set(admitted_rids)

        # ---- Evidence admission pass --------------------------------------
        # Eligible: top-K scored triples that were admitted.
        top_k_rids = {
            int(scored[i]["relation_id"])  # type: ignore[call-overload]
            for i in range(min(top_evidence_k, len(scored)))
        }
        evidence_admitted: dict[
            int, list[tuple[int, str, str | None, str | None, str | None, str | None]]
        ] = {}

        for item in scored:
            rid = int(item["relation_id"])  # type: ignore[call-overload]
            if rid not in top_k_rids or rid not in admitted_set:
                continue
            evs: list[tuple[int, str, str | None, str | None, str | None, str | None]] = list(
                item["evidence"]  # type: ignore[call-overload]
            )
            admitted_snippets: list[
                tuple[int, str, str | None, str | None, str | None, str | None]
            ] = []
            for ev in evs:
                _doc_id, content, proj, sp, section, doc_date = ev
                cost = est_tokens(str(content))
                if cost <= remaining:
                    admitted_snippets.append(ev)
                    remaining -= cost
            if admitted_snippets:
                evidence_admitted[rid] = admitted_snippets

        # ---- Render -------------------------------------------------------
        # Group admitted triples by subject, preserving score order.
        subj_groups: OrderedDict[str, list[tuple[str, str, int]]] = OrderedDict()
        for rid in admitted_rids:
            item = next(x for x in scored if int(x["relation_id"]) == rid)  # type: ignore[call-overload]
            subj = str(item["subject"])
            pred = str(item["predicate"])
            obj = str(item["object"])
            subj_groups.setdefault(subj, []).append((pred, obj, rid))

        lines: list[str] = []
        for subj, preds in subj_groups.items():
            parts = [f"{pred} {obj}" for pred, obj, _ in preds]
            lines.append(f"{subj}: {' | '.join(parts)}")

        # Evidence block.
        if evidence_admitted:
            lines.append("")
            for evs in evidence_admitted.values():
                for _doc_id, content, proj, sp, section, doc_date in evs:
                    tag = _provenance_tag(proj, sp, section, doc_date)
                    lines.append(f"[{tag}]")
                    lines.append(str(content).strip())

        # Coverage footer — always appended, cost NOT deducted from budget.
        admitted_count = len(admitted_rids)
        used_tokens = budget - remaining
        lines.append("")
        suffix = "; raise --budget for more)" if admitted_count < total else ")"
        lines.append(
            f"(returned {admitted_count}/{total} triples, "
            f"~{used_tokens:,}/{budget:,} tokens{suffix}"
        )

        return "\n".join(lines)

    def fts_fallback_chunks(
        self,
        query: str,
        limit: int = 5,
        project_filter: str | None = None,
    ) -> list[tuple[int, str, str | None, str | None, str | None, str | None]]:
        """Direct FTS5 BM25 search over document chunks (seed-resolution fallback).

        Used when the query's seed entities resolve to no graph entities, or
        BFS yields no candidate relations: instead of returning an empty
        result, the raw evidence chunks are searched directly with an
        OR-of-tokens FTS5 query so the best-matching chunks still surface.

        Chunks sharing the same ``(source_path, section)`` are deduplicated,
        keeping only the highest version (latest re-ingest).  Result order is
        BM25 relevance, best match first.

        Args:
            query: Raw natural-language question.
            limit: Maximum number of chunks to return.
            project_filter: If set, restrict to documents of this project.

        Returns:
            List of ``(doc_id, content, project, source_path, section,
            doc_date)`` tuples, best match first.  Empty when FTS5 is
            unavailable or nothing matches.
        """
        if not query.strip() or limit <= 0:
            return []

        conn = self._cm.connection()  # type: ignore[attr-defined]

        if _contains_cjk(query):
            cjk_chunks = self._cjk_fts_chunks(query, limit=limit, project_filter=project_filter)
            if cjk_chunks:
                return cjk_chunks

        match_expr = _fts5_or_query(query)
        if match_expr == '""' or not _fts_table_exists(conn, "documents_fts"):
            return []
        project_clause = "" if project_filter is None else "AND d.project = ? "
        params: list[object] = [match_expr]
        if project_filter is not None:
            params.append(project_filter)
        # Over-fetch so version deduplication and source diversification can
        # still fill `limit` rows when one long source dominates BM25.
        params.append(limit * 16)

        try:
            rows = conn.execute(
                "SELECT d.id, d.content, d.project, d.source_path, d.section, "  # noqa: S608
                "       d.doc_date, d.version "
                "FROM documents_fts "
                "JOIN documents d ON d.id = documents_fts.rowid "
                "WHERE documents_fts MATCH ? "
                f"{project_clause}"
                "ORDER BY bm25(documents_fts) "
                "LIMIT ?",
                params,
            ).fetchall()
        except Exception:
            return []

        anchors = _lexical_anchor_terms(query)
        if anchors:
            rows = [
                row
                for _idx, row in sorted(
                    enumerate(rows),
                    key=lambda item: (-_lexical_row_score(item[1], anchors), item[0]),
                )
            ]
        return _dedup_chunk_rows(rows, limit=limit, query=query)

    def _cjk_fts_chunks(
        self,
        query: str,
        limit: int,
        project_filter: str | None,
    ) -> list[tuple[int, str, str | None, str | None, str | None, str | None]]:
        """Return CJK-aware FTS chunks using the trigram sidecar when available."""
        conn = self._cm.connection()  # type: ignore[attr-defined]

        trigram_terms = _cjk_trigram_terms(query)

        # If there are no trigram terms (all CJK runs are shorter than 3 chars),
        # fall back to the guarded LIKE path which handles 1-2-char runs.
        if not trigram_terms:
            return self._short_cjk_like_chunks(query, limit=limit, project_filter=project_filter)

        # Trigram terms exist but the sidecar table is absent (pre-v5 DB).
        # Return [] so the caller (fts_fallback_chunks) falls through to the
        # existing unicode61 path, preserving the pre-v5 behaviour exactly.
        if not _fts_table_exists(conn, "documents_fts_trigram"):
            return []

        match_expr = _fts5_query_from_terms(trigram_terms)
        project_clause = "" if project_filter is None else "AND d.project = ? "
        params: list[object] = [match_expr]
        if project_filter is not None:
            params.append(project_filter)
        params.append(limit * 8)
        try:
            rows = conn.execute(
                "SELECT d.id, d.content, d.project, d.source_path, d.section, "  # noqa: S608
                "       d.doc_date, d.version, bm25(documents_fts_trigram) "
                "FROM documents_fts_trigram "
                "JOIN documents d ON d.id = documents_fts_trigram.rowid "
                "WHERE documents_fts_trigram MATCH ? "
                f"{project_clause}"
                "ORDER BY bm25(documents_fts_trigram) "
                "LIMIT ?",
                params,
            ).fetchall()
        except Exception:
            rows = []

        # Sidecar present but no corpus match: return empty — do NOT degrade to
        # LIKE.  A CJK query with no real match must return [] just as it would
        # on main (unicode61 also returns nothing for an unmatched CJK query).
        if not rows:
            return []

        focus_query = _cjk_focus_query(query)
        anchors = _cjk_anchor_terms(focus_query)
        reranked = sorted(
            rows,
            key=lambda row: (
                -_cjk_row_score(row, anchors),
                float(row[7]),
            ),
        )
        return _dedup_chunk_rows(reranked, limit=limit, query=focus_query)

    def _short_cjk_like_chunks(
        self,
        query: str,
        limit: int,
        project_filter: str | None,
    ) -> list[tuple[int, str, str | None, str | None, str | None, str | None]]:
        """Guarded LIKE fallback for CJK queries without usable trigram terms."""
        conn = self._cm.connection()  # type: ignore[attr-defined]
        terms = _cjk_anchor_terms(query, min_len=1, max_len=2, limit=8)
        if not terms:
            return []

        clauses = " OR ".join("d.content LIKE ?" for _ in terms)
        project_clause = "" if project_filter is None else "AND d.project = ? "
        params: list[object] = [f"%{term}%" for term in terms]
        if project_filter is not None:
            params.append(project_filter)
        params.append(limit * 8)
        rows = conn.execute(
            "SELECT d.id, d.content, d.project, d.source_path, d.section, "  # noqa: S608
            "       d.doc_date, d.version "
            "FROM documents d "
            f"WHERE ({clauses}) "
            f"{project_clause}"
            "ORDER BY d.doc_date DESC, d.id DESC "
            "LIMIT ?",
            params,
        ).fetchall()
        focus_query = _cjk_focus_query(query)
        reranked = sorted(rows, key=lambda row: -_cjk_row_score(row, terms))
        return _dedup_chunk_rows(reranked, limit=limit, query=focus_query)

    def fts_fallback_output(
        self,
        chunks: list[tuple[int, str, str | None, str | None, str | None, str | None]],
        budget: int,
    ) -> str:
        """Render FTS-fallback chunks with token budgeting and a coverage footer.

        Mirrors the :meth:`compact_output` contract: greedy best-effort
        admission within ``budget`` (a chunk's cost is its provenance tag plus
        content), and an always-appended honest coverage footer.  The footer
        keeps the ``returned 0/0 triples`` prefix so callers and tests that
        key on the standard footer shape keep working, and additionally
        reports ``K/M FTS chunks``.

        Args:
            chunks: ``(doc_id, content, project, source_path, section,
                doc_date)`` tuples from :meth:`fts_fallback_chunks`,
                best match first.
            budget: Token budget for chunk content (footer excluded).

        Returns:
            Multi-line string of provenance-tagged chunks plus footer.
        """
        total = len(chunks)
        remaining = budget
        admitted = 0
        lines: list[str] = []

        for _doc_id, content, proj, sp, section, doc_date in chunks:
            tag = _provenance_tag(proj, sp, section, doc_date)
            text = str(content).strip()
            cost = est_tokens(f"[{tag}]") + est_tokens(text)
            if cost <= remaining:
                lines.append(f"[{tag}]")
                lines.append(text)
                admitted += 1
                remaining -= cost

        used_tokens = budget - remaining
        if lines:
            lines.append("")
        suffix = "; raise --budget for more)" if admitted < total else ")"
        lines.append(
            f"(returned 0/0 triples, {admitted}/{total} FTS chunks, "
            f"~{used_tokens:,}/{budget:,} tokens{suffix}"
        )
        return "\n".join(lines)

    def fused_output(
        self,
        scored: list[dict[str, object]],
        chunks: list[tuple[int, str, str | None, str | None, str | None, str | None]],
        budget: int,
        chunk_share: float = 0.4,
        top_evidence_k: int = 3,
    ) -> str:
        """Budget-partitioned graph+FTS fusion output (spec §6 three-pass knapsack).

        Divides the token budget between the triple pool (pass 1) and the chunk
        pool (pass 2), with leftover rolling forward and a triple backfill pass
        (pass 3) consuming any remaining budget.

        Pass 1 (triples): greedy skip-and-continue over ``scored`` (already
        sorted by :meth:`scored_query`).  Allowance = ``budget - chunk_reserve``
        where ``chunk_reserve = floor(budget * chunk_share)``.  Evidence
        snippets are attached for the top-*top_evidence_k* admitted triples,
        costs charged against this pass's allowance.

        Pass 2 (chunks): allowance = ``chunk_reserve`` + leftover from pass 1.
        Iterates ``chunks`` in given order (best-first from
        :meth:`fts_fallback_chunks`).  Cross-pool dedup: a chunk whose
        ``doc_id`` was emitted as evidence in an admitted triple is skipped.

        Pass 3 (triple backfill): remaining budget goes to un-admitted triples
        (triple lines only; evidence is not re-tried in backfill).

        The coverage footer is always appended and excluded from the budget.

        Args:
            scored: Sorted scored-triple dicts from :meth:`scored_query`.
            chunks: ``(doc_id, content, project, source_path, section,
                doc_date)`` tuples from :meth:`fts_fallback_chunks`,
                best match first.
            budget: Total token budget for all output (footer excluded).
            chunk_share: Fraction of ``budget`` reserved for chunks in pass 2.
                Default ``0.4``.
            top_evidence_k: Maximum triples eligible for evidence snippets.
                Default ``3``.

        Returns:
            Multi-line fused output: subject-grouped triples section, then
            ``Relevant source chunks`` chunk section, then coverage footer.
        """
        import math

        total_triples = len(scored)
        total_chunks = len(chunks)

        chunk_reserve = math.floor(budget * chunk_share)
        triple_allowance = budget - chunk_reserve

        # ---- Pass 1: triples (with evidence for top-K) -------------------
        admitted_rids: list[int] = []
        remaining_triple = triple_allowance

        # Identify top-K relation ids for evidence eligibility.
        top_k_rids = {
            int(scored[i]["relation_id"])  # type: ignore[call-overload]
            for i in range(min(top_evidence_k, len(scored)))
        }

        evidence_admitted: dict[
            int, list[tuple[int, str, str | None, str | None, str | None, str | None]]
        ] = {}
        # Track doc_ids emitted as evidence snippets for cross-pool dedup.
        evidence_doc_ids: set[int] = set()

        for item in scored:
            rid = int(item["relation_id"])  # type: ignore[call-overload]
            subj = str(item["subject"])
            pred = str(item["predicate"])
            obj = str(item["object"])
            line = f"{subj}: {pred} {obj}"
            cost = est_tokens(line)
            if cost <= remaining_triple:
                admitted_rids.append(rid)
                remaining_triple -= cost

                # Evidence for top-K admitted triples.
                if rid in top_k_rids:
                    evs = cast(
                        "list[tuple[int, str, str | None, str | None, str | None, str | None]]",
                        item["evidence"],
                    )
                    admitted_snippets: list[
                        tuple[int, str, str | None, str | None, str | None, str | None]
                    ] = []
                    for ev in evs:
                        doc_id_ev, content_ev, _proj, _sp, _section, _date = ev
                        ev_cost = est_tokens(str(content_ev))
                        if ev_cost <= remaining_triple:
                            admitted_snippets.append(ev)
                            evidence_doc_ids.add(int(doc_id_ev))
                            remaining_triple -= ev_cost
                    if admitted_snippets:
                        evidence_admitted[rid] = admitted_snippets

        pass1_leftover = remaining_triple

        # ---- Pass 2: chunks -----------------------------------------------
        # Allowance = chunk_reserve + leftover from pass 1.
        chunk_allowance = chunk_reserve + pass1_leftover
        admitted_chunks: list[tuple[int, str, str | None, str | None, str | None, str | None]] = []
        remaining_chunk = chunk_allowance

        for chunk in chunks:
            doc_id_c = int(chunk[0])
            # Cross-pool dedup: skip if this doc was already emitted as evidence.
            if doc_id_c in evidence_doc_ids:
                continue
            content_c = str(chunk[1]).strip()
            proj_c, sp_c, section_c, date_c = chunk[2], chunk[3], chunk[4], chunk[5]
            tag_c = _provenance_tag(proj_c, sp_c, section_c, date_c)
            cost_c = est_tokens(f"[{tag_c}]") + est_tokens(content_c)
            if cost_c <= remaining_chunk:
                admitted_chunks.append(chunk)
                remaining_chunk -= cost_c

        pass2_leftover = remaining_chunk

        # ---- Pass 3: triple backfill (lines only, no evidence) ------------
        admitted_set = set(admitted_rids)
        backfill_rids: list[int] = []
        remaining_backfill = pass2_leftover

        for item in scored:
            rid = int(item["relation_id"])  # type: ignore[call-overload]
            if rid in admitted_set:
                continue
            subj = str(item["subject"])
            pred = str(item["predicate"])
            obj = str(item["object"])
            line = f"{subj}: {pred} {obj}"
            cost = est_tokens(line)
            if cost <= remaining_backfill:
                backfill_rids.append(rid)
                remaining_backfill -= cost

        # Merge admitted triples: pass-1 admissions + pass-3 backfill.
        all_admitted_rids = admitted_rids + backfill_rids

        # ---- Render -------------------------------------------------------
        # Triples section: subject-grouped, preserving admission order.
        subj_groups: OrderedDict[str, list[tuple[str, str, int]]] = OrderedDict()
        for rid in all_admitted_rids:
            item = next(x for x in scored if int(x["relation_id"]) == rid)  # type: ignore[call-overload]
            subj = str(item["subject"])
            pred = str(item["predicate"])
            obj = str(item["object"])
            subj_groups.setdefault(subj, []).append((pred, obj, rid))

        lines: list[str] = []
        for subj, preds in subj_groups.items():
            parts = [f"{pred} {obj}" for pred, obj, _ in preds]
            lines.append(f"{subj}: {' | '.join(parts)}")

        # Evidence block (pass-1 top-K evidence only).
        if evidence_admitted:
            lines.append("")
            for evs_list in evidence_admitted.values():
                for _doc_id, content, proj, sp, section, doc_date in evs_list:
                    tag = _provenance_tag(proj, sp, section, doc_date)
                    lines.append(f"[{tag}]")
                    lines.append(str(content).strip())

        # Chunk section.
        if admitted_chunks:
            if lines:
                lines.append("")
            lines.append("Relevant source chunks")
            for chunk in admitted_chunks:
                _doc_id_c, content_c, proj_c, sp_c, section_c, date_c = chunk
                tag_c = _provenance_tag(proj_c, sp_c, section_c, date_c)
                lines.append(f"[{tag_c}]")
                lines.append(str(content_c).strip())

        # Coverage footer — always appended.
        admitted_triple_count = len(all_admitted_rids)
        admitted_chunk_count = len(admitted_chunks)
        used_tokens = budget - remaining_backfill
        lines.append("")
        triple_truncated = admitted_triple_count < total_triples
        chunk_truncated = admitted_chunk_count < total_chunks
        suffix = "; raise --budget for more)" if (triple_truncated or chunk_truncated) else ")"
        lines.append(
            f"(returned {admitted_triple_count}/{total_triples} triples, "
            f"{admitted_chunk_count}/{total_chunks} FTS chunks, "
            f"~{used_tokens:,}/{budget:,} tokens{suffix}"
        )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level helpers (also importable for tests)
# ---------------------------------------------------------------------------


def _fts5_escape(query: str) -> str:
    """Escape a query string for use in an FTS5 MATCH expression.

    Strips FTS5-special characters and wraps the result in double quotes so
    FTS5 treats it as a phrase (tokenized normally, not as raw operators).

    Args:
        query: Raw user query string.

    Returns:
        FTS5-safe MATCH expression.
    """
    cleaned = re.sub(r'["\*\^\(\)\{\}\[\]:,]', " ", query)
    cleaned = cleaned.strip()
    if not cleaned:
        return '""'
    return f'"{cleaned}"'


def _fts_table_and_query(conn: object, query: str) -> tuple[str | None, str | None]:
    """Choose the FTS table and MATCH expression for a query."""
    if _contains_cjk(query) and _fts_table_exists(conn, "documents_fts_trigram"):
        terms = _cjk_trigram_terms(query)
        if terms:
            return "documents_fts_trigram", _fts5_query_from_terms(terms)

    if not _fts_table_exists(conn, "documents_fts"):
        return None, None

    match_expr = _fts5_or_query(query)
    if match_expr == '""':
        return None, None
    return "documents_fts", match_expr


def _dedup_chunk_rows(
    rows: list[tuple[object, ...]],
    limit: int,
    query: str | None,
) -> list[tuple[int, str, str | None, str | None, str | None, str | None]]:
    """Deduplicate FTS document rows and optionally convert CJK content to excerpts."""
    chosen: dict[object, tuple[int, int, tuple[object, ...]]] = {}
    order = 0
    for row in rows:
        doc_id, _content, _proj, sp, section, _doc_date, version = row[:7]
        key: object = (sp, section) if sp else ("doc", doc_id)
        ver = int(str(version)) if version is not None else 0
        if key not in chosen:
            chosen[key] = (order, ver, tuple(row[:7]))
            order += 1
        elif ver > chosen[key][1]:
            chosen[key] = (chosen[key][0], ver, tuple(row[:7]))

    ranked_rows = [row for _, _, row in sorted(chosen.values(), key=lambda item: item[0])]
    diversified_rows = _round_robin_by_source(ranked_rows, limit=limit)

    result: list[tuple[int, str, str | None, str | None, str | None, str | None]] = []
    for row in diversified_rows:
        content = str(row[1])
        if query is not None:
            content = (
                _cjk_excerpt(query, content)
                if _contains_cjk(query)
                else _lexical_excerpt(query, content)
            )
        result.append(
            (
                int(row[0]),  # type: ignore[call-overload]
                content,
                row[2] if row[2] is None else str(row[2]),
                row[3] if row[3] is None else str(row[3]),
                row[4] if row[4] is None else str(row[4]),
                row[5] if row[5] is None else str(row[5]),
            )
        )
    return result


def _lexical_excerpt(query: str, content: str) -> str:
    """Return a compact excerpt around lexical query anchors for long chunks."""
    if est_tokens(content) <= _LEXICAL_EXCERPT_THRESHOLD_TOKENS:
        return content

    anchors = _lexical_anchor_terms(query)
    if not anchors:
        return content

    lowered = content.lower()
    windows: list[tuple[int, int, set[str], int]] = []
    for anchor in anchors:
        pos = lowered.find(anchor)
        while pos != -1:
            start = max(0, pos - _LEXICAL_EXCERPT_BEFORE_CHARS)
            end = min(len(content), pos + len(anchor) + _LEXICAL_EXCERPT_AFTER_CHARS)
            covered = {term for term in anchors if term in lowered[start:end]}
            windows.append((start, end, covered, len(covered)))
            pos = lowered.find(anchor, pos + len(anchor))

    if not windows:
        return content

    windows.sort(key=lambda item: (-item[3], item[0]))
    selected: list[tuple[int, int, set[str], int]] = []
    covered_terms: set[str] = set()
    for window in windows:
        new_terms = window[2] - covered_terms
        if not selected or new_terms:
            selected.append(window)
            covered_terms.update(window[2])
        if len(selected) >= _LEXICAL_EXCERPT_WINDOWS:
            break

    spans = _merge_spans((start, end) for start, end, _covered, _score in selected)
    excerpts = [content[start:end].strip() for start, end in spans]
    if not excerpts:
        return content
    return "[excerpt]\n" + "\n...\n".join(excerpts)


def _lexical_anchor_terms(query: str, limit: int = 24) -> list[str]:
    """Return lowercase lexical terms useful for excerpt windows."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_#.+/-]+", query.lower()):
        term = raw.strip("._/-")
        if len(term) < 3 or term in _LEXICAL_STOPWORDS or term in seen:
            continue
        terms.append(term)
        seen.add(term)
        if len(terms) >= limit:
            break
    return terms


def _lexical_row_score(row: tuple[object, ...], terms: list[str]) -> int:
    """Score a non-CJK FTS row by distinct lexical anchor coverage."""
    content = str(row[1]).lower()
    return sum(len(term) for term in terms if term in content)


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping excerpt spans."""
    ordered = sorted(spans)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _round_robin_by_source(rows: list[tuple[object, ...]], limit: int) -> list[tuple[object, ...]]:
    """Preserve ranking while preventing one source from monopolising chunks."""
    if limit <= 0 or len(rows) <= limit:
        return rows[:limit]

    buckets: OrderedDict[object, list[tuple[object, ...]]] = OrderedDict()
    for row in rows:
        source_key: object = row[3] if row[3] is not None else (row[2], row[0])
        buckets.setdefault(source_key, []).append(row)

    if len(buckets) <= 1:
        return rows[:limit]

    result: list[tuple[object, ...]] = []
    while len(result) < limit and buckets:
        empty_keys: list[object] = []
        for key, bucket in buckets.items():
            if not bucket:
                empty_keys.append(key)
                continue
            result.append(bucket.pop(0))
            if len(result) >= limit:
                break
        for key in empty_keys:
            buckets.pop(key, None)

    return result


def _cjk_focus_query(query: str) -> str:
    """Return the CJK portion most useful for reranking and excerpts."""
    focus = query
    for marker in ("项目中", "项目里", "项目的"):
        if marker in focus:
            focus = focus.split(marker, 1)[1]
            break
    for marker in ("是什么", "是哪个", "有哪些", "?", "\uff1f"):
        if marker in focus:
            focus = focus.split(marker, 1)[0]
            break
    return focus.strip() or query


def _cjk_content_score(content: str, terms: list[str]) -> int:
    """Score a CJK candidate document by weighted distinct-term coverage.

    Only *maximal* matched terms count: a matched term that is a substring of
    a longer matched term is skipped, so a document repeating one short
    fragment of the query (e.g. ``命格``) cannot outrank a document covering
    more distinct query concepts.  Longer terms weigh more (their length).

    Args:
        content: Candidate document content.
        terms: Anchor n-grams from :func:`_cjk_anchor_terms` (unique).

    Returns:
        Sum of lengths of maximal matched terms (0 when nothing matches).
    """
    matched = [term for term in terms if term in content]
    return sum(
        len(term)
        for term in matched
        if not any(term != other and term in other for other in matched)
    )


def _cjk_row_score(row: tuple[object, ...], terms: list[str]) -> int:
    """Score a CJK row, lightly preferring current-state handoff sections."""
    score = _cjk_content_score(str(row[1]), terms)
    section = "" if row[4] is None else str(row[4]).lower()
    if score > 0 and ("current state" in section or "当前" in section):
        score += 3
    return score


def _cjk_excerpt(query: str, content: str) -> str:
    """Return a compact CJK query-centered excerpt for an FTS chunk."""
    text = content.strip()
    anchors = _cjk_anchor_terms(query)
    if not anchors:
        return text

    candidates: list[tuple[int, int, int, int]] = []
    lower_text = text.lower()
    if "当前阶段" in query and (phase_pos := lower_text.find("current phase")) >= 0:
        start = max(0, phase_pos - _CJK_EXCERPT_BEFORE_CHARS)
        end = min(len(text), phase_pos + _CJK_EXCERPT_AFTER_CHARS)
        start, end = _expand_cjk_span(text, start, end)
        score = _cjk_content_score(text[start:end], anchors) + 12
        candidates.append((score, start, end, phase_pos))

    for term in anchors:
        start_at = 0
        while True:
            pos = text.find(term, start_at)
            if pos < 0:
                break
            start = max(0, pos - _CJK_EXCERPT_BEFORE_CHARS)
            end = min(len(text), pos + _CJK_EXCERPT_AFTER_CHARS)
            start, end = _expand_cjk_span(text, start, end)
            window = text[start:end]
            score = _cjk_content_score(window, anchors)
            candidates.append((score, start, end, pos))
            start_at = pos + 1

    if not candidates:
        return text

    selected: list[tuple[int, int]] = []
    covered: set[str] = set()
    best_score = 0
    for score, start, end, _pos in sorted(candidates, key=lambda item: (-item[0], item[1])):
        overlaps = any(
            not (end <= sel_start or start >= sel_end) for sel_start, sel_end in selected
        )
        if overlaps:
            continue
        # Marginal-gain gate: an extra window must either contribute an anchor
        # term not already covered by the selected windows, or carry at least
        # half the anchor weight of the best window.  Weak redundant windows
        # are dropped so excerpts stay compact (downstream admission is
        # token-budget-constrained) instead of padding to
        # _CJK_EXCERPT_WINDOWS with low-signal context.
        window_terms = {term for term in anchors if term in text[start:end]}
        if selected and window_terms <= covered and score * 2 < best_score:
            continue
        selected.append((start, end))
        covered |= window_terms
        best_score = max(best_score, score)
        if len(selected) >= _CJK_EXCERPT_WINDOWS:
            break

    if not selected:
        return text

    parts: list[str] = []
    for start, end in sorted(selected):
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        parts.append(snippet)

    excerpt = "\n\n[excerpt]\n" + "\n\n...\n\n".join(parts)
    return excerpt if est_tokens(excerpt) < est_tokens(text) else text


def _expand_cjk_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a CJK excerpt window to nearby sentence or line boundaries."""
    left_candidates = [text.rfind(mark, 0, start) for mark in ("\n", "。", "\uff1b", ";")]
    left = max(left_candidates)
    if left >= 0:
        start = left + 1

    right_candidates = [
        pos for mark in ("。", "\uff1b", ";", "\n") if (pos := text.find(mark, end)) >= 0
    ]
    if right_candidates:
        end = min(right_candidates) + 1

    return start, end


def _best_doc_date(
    evidence: list[tuple[int, str, str | None, str | None, str | None, str | None]],
) -> str:
    """Return the lexicographically latest doc_date from an evidence list.

    Args:
        evidence: List of ``(doc_id, content, project, source_path, section, doc_date)``.

    Returns:
        Latest ISO-8601 date string, or empty string if none available.
    """
    dates = [ev[5] for ev in evidence if ev[5]]
    if not dates:
        return ""
    return max(dates)


def _date_sort_key(date_str: str) -> str:
    """Inverted sort key so newest dates sort first (used for tie-breaking).

    Args:
        date_str: ISO-8601 date string or empty string.

    Returns:
        Inverted key: each digit d is replaced by (9 - d); empty string sorts last.
    """
    if not date_str:
        return "z"  # sorts after any date string
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in date_str)


def _provenance_tag(
    project: str | None,
    source_path: str | None,
    section: str | None,
    doc_date: str | None,
) -> str:
    """Build a compact provenance tag from document metadata fields.

    Format: ``project source_path ## section doc_date`` (fields omitted when None).

    Args:
        project: Project name.
        source_path: Canonical file path.
        section: Section heading (without leading ``##``).
        doc_date: ISO-8601 date string.

    Returns:
        Non-empty provenance tag string.
    """
    parts: list[str] = []
    if project:
        parts.append(project)
    if source_path:
        parts.append(source_path)
    if section:
        parts.append(f"## {section}")
    if doc_date:
        parts.append(doc_date)
    return " ".join(parts) if parts else "unknown"
