"""membox store — SQLite-backed knowledge graph store. Phase 1: typed stubs."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from membox.embed import Embedder
    from membox.schema import Entity, HopResult, Relation

_P2 = "Phase 2: not yet implemented"
_P4 = "Phase 4: not yet implemented"
_P5 = "Phase 5: not yet implemented"


class KnowledgeStore:
    """Thread-safe SQLite-backed knowledge graph store.

    Uses per-thread connections, WAL mode, and an RLock to guard the
    find-or-create entity critical section against concurrent writers.
    """

    def __init__(self, db_path: str = "memory.db") -> None:
        self.db_path = db_path
        self._local = threading.local()
        # Serializes the non-atomic "find-or-create entity" critical section.
        self._write_lock = threading.RLock()
        self._init_schema()

    # ---- connection management ----

    def _conn(self) -> sqlite3.Connection:
        """Return the per-thread SQLite connection, creating it if needed."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        """Context manager for explicit transactions with automatic rollback on error."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        else:
            conn.execute("COMMIT;")

    def _init_schema(self) -> None:
        """Create tables and indexes. Phase 1: stub (no-op). Phase 2 implements DDL."""
        pass

    # ---- document writes ----

    def insert_document(self, content: str, source: str = "") -> int:
        """Insert a document and return its id.

        Args:
            content: Raw document text.
            source: Optional source identifier (file path, URL, etc.).

        Returns:
            New document id.
        """
        raise NotImplementedError(_P2)

    # ---- entity reads/writes ----

    def find_entity_by_alias(self, alias: str) -> int | None:
        """Return entity_id for an exact alias match, or None.

        Args:
            alias: Lowercased, whitespace-collapsed alias to look up.

        Returns:
            entity_id if found, else None.
        """
        raise NotImplementedError(_P2)

    def find_similar_entity(
        self,
        embedding: list[float],
        type_hint: str | None,
        threshold: float = 0.85,
    ) -> int | None:
        """Return entity_id of an existing entity with cosine similarity ≥ threshold, or None.

        Args:
            embedding: Query embedding vector.
            type_hint: Restrict scan to entities of this type; None to scan all.
            threshold: Minimum cosine similarity to consider a match.

        Returns:
            entity_id if a match is found, else None.
        """
        raise NotImplementedError(_P2)

    def create_entity(
        self,
        canonical: str,
        type_: str,
        description: str,
        embedding: list[float] | None,
    ) -> int:
        """Insert a new entity and return its id.

        Args:
            canonical: Canonical display name.
            type_: Entity type string.
            description: Short description.
            embedding: Optional embedding vector.

        Returns:
            New entity id.
        """
        raise NotImplementedError(_P2)

    def add_alias(self, alias: str, entity_id: int) -> None:
        """Register an alias for an existing entity.

        Args:
            alias: Alias string to register (will be normalized).
            entity_id: Target entity.
        """
        raise NotImplementedError(_P2)

    def update_entity_description(self, entity_id: int, new_desc: str) -> None:
        """Replace the entity description if new_desc is longer (keep-longer heuristic).

        Args:
            entity_id: Target entity.
            new_desc: Candidate new description.
        """
        raise NotImplementedError(_P2)

    def find_or_create_entity(
        self,
        name: str,
        type_: str,
        description: str,
        embedder: Embedder | None,
    ) -> int:
        """Resolve name to an entity_id via alias → embedding → create. Thread-safe via RLock.

        Resolution order:
          1. Exact alias match (cheap, deterministic).
          2. Embedding cosine ≥ 0.85 within same type (requires embedder).
          3. Create new entity.

        Args:
            name: Entity surface form.
            type_: Entity type string.
            description: Short description for the entity.
            embedder: Optional embedder for fuzzy matching; None falls back to string-only.

        Returns:
            entity_id (existing or newly created).
        """
        raise NotImplementedError(_P4)

    # ---- relation writes ----

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
        raise NotImplementedError(_P2)

    # ---- graph reads ----

    def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None:
        """Return (id, canonical_name, type, description) or None.

        Args:
            entity_id: Entity to look up.

        Returns:
            4-tuple or None if not found.
        """
        raise NotImplementedError(_P2)

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
        raise NotImplementedError(_P2)

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
        raise NotImplementedError(_P2)

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
        raise NotImplementedError(_P5)

    # ---- list views ----

    def list_entities(self) -> list[Entity]:
        """Return all entities in the graph.

        Returns:
            List of Entity objects ordered by id.
        """
        raise NotImplementedError(_P2)

    def list_relations(self) -> list[Relation]:
        """Return all relations with source and target names resolved.

        Returns:
            List of Relation objects with source_name and target_name filled in.
        """
        raise NotImplementedError(_P2)
