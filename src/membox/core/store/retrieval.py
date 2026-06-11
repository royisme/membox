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
from typing import TYPE_CHECKING

# est_tokens is defined in core.tokens and re-exported here for backward
# compatibility: callers that import it from this module continue to work.
from membox.core.tokens import est_tokens as est_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable

    from membox.config import RetrievalConfig
    from membox.model.schema import HopResult

# BFS depth for seed entities.
_SEED_DEPTH = 0


class RetrievalOps:
    """BFS retrieval operations, mixed into :class:`KnowledgeStore`.

    Relies on the entity and relation mixins for ``get_entity``,
    ``get_neighbors``, ``get_evidence_docs``, and ``get_evidence_docs_with_meta``.
    """

    # Provided by sibling mixins (declared for type checking).
    if TYPE_CHECKING:

        def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None: ...

        def get_neighbors(self, entity_ids: Iterable[int]) -> list[tuple[int, int, int, str]]: ...

        def get_evidence_docs(self, relation_ids: Iterable[int]) -> list[tuple[int, int, str]]: ...

        def get_evidence_docs_with_meta(
            self, relation_ids: Iterable[int]
        ) -> list[tuple[int, int, str, str | None, str | None, str | None, str | None]]: ...

        def get_relation_embedding(self, relation_id: int) -> list[float] | None: ...

    def bfs_query(
        self,
        seed_ids: list[int],
        max_hops: int,
    ) -> HopResult:
        """BFS from seed_ids for up to max_hops. Returns traversal result with lineage.

        Args:
            seed_ids: Starting entity ids.
            max_hops: Maximum number of BFS expansions.

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
            edges = self.get_neighbors(frontier)
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
    ) -> tuple[
        dict[int, tuple[int, int, int, str]],
        dict[int, int],
        dict[int, str],
    ]:
        """BFS from seed_ids preserving per-entity BFS depth for hop scoring.

        Args:
            seed_ids: Starting entity ids.
            max_hops: Maximum number of BFS expansions.

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
            edges = self.get_neighbors(frontier)
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

        # Check FTS5 table exists (absent before migration 3 on old DBs).
        has_fts = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts';"
            ).fetchone()
        )
        if not has_fts:
            return {}

        placeholders = ",".join("?" * len(relation_ids))
        safe_query = _fts5_escape(query)

        try:
            rows = conn.execute(
                f"SELECT re.relation_id, -bm25(documents_fts) AS score "  # noqa: S608
                f"FROM documents_fts "
                f"JOIN relation_evidence re ON re.doc_id = documents_fts.rowid "
                f"WHERE documents_fts MATCH ? "
                f"  AND re.relation_id IN ({placeholders})",
                [safe_query, *relation_ids],
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

        Returns:
            List of scored dicts sorted descending by score with deterministic
            tie-breaking (hops asc, then newest doc_date desc).
        """
        from membox.config import RetrievalConfig
        from membox.core.store.entities import _cosine

        cfg = config or RetrievalConfig()
        hop_decay = cfg.hop_decay
        alpha = cfg.alpha

        # 1. BFS with depth tracking.
        collected, entity_depth, name_cache = self.bfs_query_with_depths(seed_ids, max_hops)
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
                        cos = _cosine(query_embedding, rel_blob)
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

        # Check FTS5 table exists (absent before migration 3 on old DBs).
        has_fts = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts';"
            ).fetchone()
        )
        if not has_fts:
            return []

        match_expr = _fts5_or_query(query)
        project_clause = "" if project_filter is None else "AND d.project = ? "
        params: list[object] = [match_expr]
        if project_filter is not None:
            params.append(project_filter)
        # Over-fetch so version deduplication can still fill `limit` rows.
        params.append(limit * 4)

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

        # Dedup re-ingested chunks: keep the highest version per
        # (source_path, section), preserving best-score-first group order.
        chosen: dict[object, tuple[int, int, tuple[object, ...]]] = {}
        order = 0
        for row in rows:
            doc_id, _content, _proj, sp, section, _doc_date, version = row
            key: object = (sp, section) if sp else ("doc", doc_id)
            ver = int(version) if version is not None else 0
            if key not in chosen:
                chosen[key] = (order, ver, tuple(row))
                order += 1
            elif ver > chosen[key][1]:
                chosen[key] = (chosen[key][0], ver, tuple(row))

        result = [
            (
                int(r[0]),  # type: ignore[call-overload]
                str(r[1]),
                r[2],
                r[3],
                r[4],
                r[5],
            )
            for _, _, r in sorted(chosen.values(), key=lambda item: item[0])
        ]
        return result[:limit]  # type: ignore[return-value]

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


def _fts5_or_query(query: str) -> str:
    """Build an OR-of-tokens FTS5 MATCH expression from a natural-language query.

    A phrase match (as produced by :func:`_fts5_escape`) requires every query
    token to appear contiguously, which almost never holds for a full
    question against prose chunks.  For fallback seeding each token is quoted
    individually and joined with ``OR`` so any matching token surfaces a
    chunk, and BM25 ranks chunks matching more (and rarer) tokens higher.

    Args:
        query: Raw user query string.

    Returns:
        FTS5-safe MATCH expression, e.g. ``"what" OR "storage" OR "backend"``.
    """
    cleaned = re.sub(r'["\*\^\(\)\{\}\[\]:,?!。?!、;;\.]', " ", query)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


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
