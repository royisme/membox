"""BFS multi-hop graph retrieval."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from membox.model.schema import HopResult


class RetrievalOps:
    """BFS retrieval operations, mixed into :class:`KnowledgeStore`.

    Relies on the entity and relation mixins for ``get_entity``,
    ``get_neighbors``, and ``get_evidence_docs``.
    """

    # Provided by sibling mixins (declared for type checking).
    if TYPE_CHECKING:

        def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None: ...

        def get_neighbors(self, entity_ids: Iterable[int]) -> list[tuple[int, int, int, str]]: ...

        def get_evidence_docs(self, relation_ids: Iterable[int]) -> list[tuple[int, int, str]]: ...

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
