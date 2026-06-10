"""Document persistence for evidence lineage.

Provides :class:`DocumentOps`, a mixin for :class:`~membox.core.store.KnowledgeStore`
that handles document insertion with full M2 metadata support (project, source_path,
section, doc_date, version) and idempotent re-ingestion versioning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from membox.core.store.connection import ConnectionManager


class DocumentOps:
    """Document write operations, mixed into :class:`KnowledgeStore`."""

    _cm: ConnectionManager

    def next_version_for(self, source_path: str) -> int:
        """Return the next version number for a given source path.

        Re-ingesting the same ``source_path`` increments the version so each
        ingest round is distinguishable without destroying prior evidence.

        Args:
            source_path: Canonical file path of the originating document.

        Returns:
            1 if ``source_path`` has never been ingested, else max(existing) + 1.
        """
        conn = self._cm.connection()
        row = conn.execute(
            "SELECT MAX(version) FROM documents WHERE source_path = ?;",
            (source_path,),
        ).fetchone()
        current_max: int | None = row[0] if row else None
        return 1 if current_max is None else current_max + 1

    def insert_document(
        self,
        content: str,
        source: str = "",
        *,
        project: str | None = None,
        source_path: str | None = None,
        section: str | None = None,
        doc_date: str | None = None,
        version: int | None = None,
    ) -> int:
        """Insert a document and return its id.

        When ``source_path`` is provided and ``version`` is omitted the method
        automatically computes the next version number (idempotent re-ingest
        creates a new version row; no existing rows are deleted or modified).

        Args:
            content: Raw document text.
            source: Source identifier (file path, URL, etc.) — legacy field
                kept for backward compatibility.
            project: Repository / directory name for ``--project`` scoping.
            source_path: Canonical file path of the originating document.
            section: Section heading if the document was chunked by heading.
            doc_date: ISO-8601 date string of the document snapshot.
            version: Explicit version number.  If *None* and ``source_path``
                is provided, the next version is computed automatically.

        Returns:
            New document id.
        """
        if source_path is not None and version is None:
            version = self.next_version_for(source_path)

        with self._cm.transaction() as c:
            cur = c.execute(
                """
                INSERT INTO documents
                    (content, source, project, source_path, section, doc_date, version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (content, source, project, source_path, section, doc_date, version),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]
