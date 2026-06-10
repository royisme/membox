"""Relation persistence: relation CRUD and evidence links."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from membox.core.store.connection import ConnectionManager
    from membox.model.schema import Relation


class RelationOps:
    """Relation read/write operations, mixed into :class:`KnowledgeStore`."""

    _cm: ConnectionManager

    def upsert_relation(
        self,
        source_id: int,
        target_id: int,
        predicate: str,
        doc_id: int,
    ) -> int:
        """Insert the relation if absent and link doc as evidence. Returns relation_id.

        The (source_id, target_id, predicate) triple is unique; duplicate calls
        append evidence without creating duplicate relation rows.

        Args:
            source_id: Source entity id.
            target_id: Target entity id.
            predicate: Normalized predicate string.
            doc_id: Document providing evidence for this relation.

        Returns:
            relation_id (existing or newly created).
        """
        with self._cm.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO relations(source_id, target_id, predicate) VALUES (?, ?, ?)",
                (source_id, target_id, predicate),
            )
            row = c.execute(
                "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate=?",
                (source_id, target_id, predicate),
            ).fetchone()
            rid = int(row[0])
            c.execute(
                "INSERT OR IGNORE INTO relation_evidence(relation_id, doc_id) VALUES (?, ?)",
                (rid, doc_id),
            )
            return rid

    def get_neighbors(
        self,
        entity_ids: Iterable[int],
    ) -> list[tuple[int, int, int, str]]:
        """Return edges incident to entity_ids as (rid, source_id, target_id, predicate).

        Args:
            entity_ids: Set of entity ids to expand.

        Returns:
            List of (relation_id, source_id, target_id, predicate) tuples.
        """
        ids = list(entity_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = (
            self._cm.connection()
            .execute(
                f"SELECT id, source_id, target_id, predicate FROM relations "  # noqa: S608
                f"WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                ids + ids,
            )
            .fetchall()
        )
        return [(int(r[0]), int(r[1]), int(r[2]), str(r[3])) for r in rows]

    def get_evidence_docs(
        self,
        relation_ids: Iterable[int],
    ) -> list[tuple[int, int, str]]:
        """Return (relation_id, doc_id, content) for the given relation ids.

        Args:
            relation_ids: Relation ids to fetch evidence for.

        Returns:
            List of (relation_id, doc_id, content) tuples.
        """
        ids = list(relation_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = (
            self._cm.connection()
            .execute(
                f"SELECT re.relation_id, d.id, d.content "  # noqa: S608
                f"FROM relation_evidence re JOIN documents d ON d.id = re.doc_id "
                f"WHERE re.relation_id IN ({placeholders})",
                ids,
            )
            .fetchall()
        )
        return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]

    def list_relations(self) -> list[Relation]:
        """Return all relations with source and target names resolved.

        Returns:
            List of Relation objects with source_name and target_name filled in.
        """
        from membox.model.schema import Relation as RelationModel

        rows = (
            self._cm.connection()
            .execute(
                "SELECT r.id, r.source_id, r.target_id, r.predicate, "
                "       e1.canonical_name, e2.canonical_name "
                "FROM relations r "
                "JOIN entities e1 ON e1.id = r.source_id "
                "JOIN entities e2 ON e2.id = r.target_id "
                "ORDER BY r.id"
            )
            .fetchall()
        )
        return [
            RelationModel(
                id=int(r[0]),
                source_id=int(r[1]),
                target_id=int(r[2]),
                predicate=str(r[3]),
                source_name=str(r[4]),
                target_name=str(r[5]),
            )
            for r in rows
        ]
