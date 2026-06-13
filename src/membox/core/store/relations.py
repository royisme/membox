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
        embedding: list[float] | None = None,
    ) -> int:
        """Insert the relation if absent and link doc as evidence. Returns relation_id.

        The (source_id, target_id, predicate) triple is unique; duplicate calls
        append evidence without creating duplicate relation rows.  When
        ``embedding`` is supplied and the relation row is newly created (or the
        stored embedding is NULL), the value is written to
        ``relations.embedding`` as packed float32 bytes.

        After inserting/linking evidence, **supersession detection** runs inside
        the same transaction (M4).  Detection requires the evidence document to
        have a non-NULL ``source_path`` and non-NULL ``version``; if either is
        missing, supersession is skipped.  The rule implemented is
        **forward-only**: the new relation supersedes older-version relations
        that assert a *different* object for the same ``source_id + predicate``
        and whose evidence comes from the same ``source_path`` at a strictly
        lower ``version``.  Each old relation is marked with
        ``superseded_by = <new_rid>``.  The UPDATE is conditioned on
        ``superseded_by IS NULL`` so a lost race against a concurrent writer
        (zero rows affected) is silently ignored.

        Args:
            source_id: Source entity id.
            target_id: Target entity id.
            predicate: Normalized predicate string.
            doc_id: Document providing evidence for this relation.
            embedding: Optional precomputed triple embedding (float32 vector).
                Stored as ``struct.pack("Nf", ...)`` bytes (same encoding as
                ``entities.embedding``).  Computed by the agent layer from the
                triple rendered as ``"subject predicate object"`` plain text.

        Returns:
            relation_id (existing or newly created).
        """
        from membox.core.store.vectors import vec_to_blob

        blob = vec_to_blob(embedding) if embedding else None
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
            # Store embedding if provided and not yet set on this relation.
            if blob is not None:
                c.execute(
                    "UPDATE relations SET embedding=? WHERE id=? AND embedding IS NULL;",
                    (blob, rid),
                )
            c.execute(
                "INSERT OR IGNORE INTO relation_evidence(relation_id, doc_id) VALUES (?, ?)",
                (rid, doc_id),
            )

            # --- M4 supersession detection (forward-only) ---
            # Fetch source_path and version for the evidence document.
            doc_row = c.execute(
                "SELECT source_path, version FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if doc_row is not None:
                new_source_path = doc_row[0]
                new_version = doc_row[1]
                if new_source_path is not None and new_version is not None:
                    # Find active sibling relations: same source_id + predicate,
                    # different target_id, not yet superseded, with evidence from
                    # the same source_path at a strictly lower version.
                    old_rows = c.execute(
                        """
                        SELECT DISTINCT r.id
                        FROM relations r
                        JOIN relation_evidence re ON re.relation_id = r.id
                        JOIN documents d ON d.id = re.doc_id
                        WHERE r.source_id = ?
                          AND r.predicate = ?
                          AND r.target_id != ?
                          AND r.superseded_by IS NULL
                          AND r.id != ?
                          AND d.source_path = ?
                          AND d.version IS NOT NULL
                          AND d.version < ?
                        """,
                        (source_id, predicate, target_id, rid, new_source_path, new_version),
                    ).fetchall()
                    for (old_rid,) in old_rows:
                        c.execute(
                            "UPDATE relations SET superseded_by = ? "
                            "WHERE id = ? AND superseded_by IS NULL",
                            (rid, old_rid),
                        )

            return rid

    def get_neighbors(
        self,
        entity_ids: Iterable[int],
        *,
        include_superseded: bool = False,
    ) -> list[tuple[int, int, int, str]]:
        """Return edges incident to entity_ids as (rid, source_id, target_id, predicate).

        Args:
            entity_ids: Set of entity ids to expand.
            include_superseded: When False (default), superseded relations are
                excluded from the result.  Pass True to include them (e.g. for
                auditing via ``--include-superseded``).

        Returns:
            List of (relation_id, source_id, target_id, predicate) tuples.
        """
        ids = list(entity_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        superseded_clause = "" if include_superseded else " AND superseded_by IS NULL"
        rows = (
            self._cm.connection()
            .execute(
                f"SELECT id, source_id, target_id, predicate FROM relations "  # noqa: S608
                f"WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))"
                f"{superseded_clause}",
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

    def get_evidence_docs_with_meta(
        self,
        relation_ids: Iterable[int],
    ) -> list[tuple[int, int, str, str | None, str | None, str | None, str | None]]:
        """Return evidence docs with full metadata for scoring and provenance tags.

        Args:
            relation_ids: Relation ids to fetch evidence for.

        Returns:
            List of ``(relation_id, doc_id, content, project, source_path,
            section, doc_date)`` tuples.  Metadata fields may be None when the
            document was ingested without scoping metadata.
        """
        ids = list(relation_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = (
            self._cm.connection()
            .execute(
                f"SELECT re.relation_id, d.id, d.content, "  # noqa: S608
                f"       d.project, d.source_path, d.section, d.doc_date "
                f"FROM relation_evidence re JOIN documents d ON d.id = re.doc_id "
                f"WHERE re.relation_id IN ({placeholders})",
                ids,
            )
            .fetchall()
        )
        return [
            (
                int(r[0]),
                int(r[1]),
                str(r[2]),
                str(r[3]) if r[3] is not None else None,
                str(r[4]) if r[4] is not None else None,
                str(r[5]) if r[5] is not None else None,
                str(r[6]) if r[6] is not None else None,
            )
            for r in rows
        ]

    def get_relation_embedding(self, relation_id: int) -> list[float] | None:
        """Return the stored float32 embedding for a relation, or None.

        Args:
            relation_id: Relation to look up.

        Returns:
            Float vector, or None if no embedding has been stored.
        """
        from membox.core.store.vectors import blob_to_vec

        row = (
            self._cm.connection()
            .execute("SELECT embedding FROM relations WHERE id=?;", (relation_id,))
            .fetchone()
        )
        if row is None or row[0] is None:
            return None
        return blob_to_vec(bytes(row[0]))

    def list_relations(self) -> list[Relation]:
        """Return all relations with source and target names resolved.

        Returns:
            List of Relation objects with source_name, target_name, and
            superseded_by filled in.
        """
        from membox.model.schema import Relation as RelationModel

        rows = (
            self._cm.connection()
            .execute(
                "SELECT r.id, r.source_id, r.target_id, r.predicate, "
                "       e1.canonical_name, e2.canonical_name, r.superseded_by "
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
                superseded_by=int(r[6]) if r[6] is not None else None,
            )
            for r in rows
        ]
