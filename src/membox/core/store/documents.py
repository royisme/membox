"""Document persistence for evidence lineage."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from membox.core.store.connection import ConnectionManager


class DocumentOps:
    """Document write operations, mixed into :class:`KnowledgeStore`."""

    _cm: ConnectionManager

    def insert_document(self, content: str, source: str = "") -> int:
        """Insert a document and return its id.

        Args:
            content: Raw document text.
            source: Optional source identifier (file path, URL, etc.).

        Returns:
            New document id.
        """
        with self._cm.transaction() as c:
            cur = c.execute(
                "INSERT INTO documents(content, source) VALUES (?, ?)",
                (content, source),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]
