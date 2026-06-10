"""membox store — SQLite-backed knowledge graph store.

The :class:`KnowledgeStore` facade composes the per-concern operation mixins
(documents, entities, relations, retrieval) over a shared
:class:`~membox.core.store.connection.ConnectionManager`. Its public method
surface is identical to the historical single-file ``store.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from membox.core.store.connection import ConnectionManager
from membox.core.store.documents import DocumentOps
from membox.core.store.entities import EntityOps, _blob_to_vec, _cosine, _vec_to_blob
from membox.core.store.meta_guard import check_embedder_guard, record_embedder_meta
from membox.core.store.migrations import apply_migrations
from membox.core.store.relations import RelationOps
from membox.core.store.retrieval import RetrievalOps

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator

    from membox.services.embedding import Embedder

__all__ = [
    "KnowledgeStore",
    "_blob_to_vec",
    "_cosine",
    "_vec_to_blob",
]


class KnowledgeStore(DocumentOps, EntityOps, RelationOps, RetrievalOps):
    """Thread-safe SQLite-backed knowledge graph store.

    Uses per-thread connections, WAL mode, and an RLock to guard the
    find-or-create entity critical section against concurrent writers.
    Cross-process races on entity creation are resolved via the UNIQUE
    constraints (see find_or_create_entity). Supports use as a context
    manager; ``close()``/``__exit__`` release the calling thread's connection.

    Schema setup is migration-driven: opening a store applies all pending
    migrations (tracked via ``PRAGMA user_version``).
    """

    def __init__(
        self,
        db_path: str = "memory.db",
        embedder: Embedder | None = None,
    ) -> None:
        self.db_path = db_path
        self._cm = ConnectionManager(db_path)
        apply_migrations(self._cm.connection())
        # M3 meta guard: record on first embed-enabled open; raise on mismatch.
        if embedder is not None:
            conn = self._cm.connection()
            check_embedder_guard(
                conn, embedder.model if hasattr(embedder, "model") else "", embedder.dim
            )
            record_embedder_meta(
                conn, embedder.model if hasattr(embedder, "model") else "", embedder.dim
            )

    # ---- connection management (compatibility shims over ConnectionManager) ----

    def _conn(self) -> sqlite3.Connection:
        """Return the per-thread SQLite connection, creating it if needed."""
        return self._cm.connection()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        """Context manager for explicit transactions with automatic rollback on error."""
        with self._cm.transaction() as conn:
            yield conn

    def close(self) -> None:
        """Close the current thread's SQLite connection, if open.

        Connections are per-thread: this only closes the connection owned by
        the calling thread. Other threads' connections are released when their
        thread dies or when they call ``close()`` themselves. A subsequent
        operation on this thread transparently reopens a fresh connection.
        """
        self._cm.close()

    def __enter__(self) -> KnowledgeStore:
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the current thread's connection on context exit."""
        self.close()
