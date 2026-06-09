"""membox store — SQLite-backed knowledge graph store."""

from __future__ import annotations

import math
import sqlite3
import struct
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from membox.embed import Embedder
    from membox.schema import Entity, HopResult, Relation

_P4 = "Phase 4: not yet implemented"
_P5 = "Phase 5: not yet implemented"


# ---- vector helpers --------------------------------------------------------


def _vec_to_blob(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _blob_to_vec(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def _cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return s / (na * nb)


# ---- DDL -------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL DEFAULT 'thing',
    description     TEXT NOT NULL DEFAULT '',
    embedding       BLOB,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias       TEXT    PRIMARY KEY,
    entity_id   INTEGER NOT NULL,
    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    predicate   TEXT    NOT NULL,
    UNIQUE(source_id, target_id, predicate),
    FOREIGN KEY(source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS relation_evidence (
    relation_id INTEGER NOT NULL,
    doc_id      INTEGER NOT NULL,
    PRIMARY KEY (relation_id, doc_id),
    FOREIGN KEY(relation_id) REFERENCES relations(id) ON DELETE CASCADE,
    FOREIGN KEY(doc_id)      REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rel_src  ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_tgt  ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_alias_eid ON entity_aliases(entity_id);
"""


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
        """Create tables and indexes."""
        self._conn().executescript(_DDL)

    # ---- document writes ----

    def insert_document(self, content: str, source: str = "") -> int:
        """Insert a document and return its id.

        Args:
            content: Raw document text.
            source: Optional source identifier (file path, URL, etc.).

        Returns:
            New document id.
        """
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO documents(content, source) VALUES (?, ?)",
                (content, source),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    # ---- entity reads/writes ----

    def find_entity_by_alias(self, alias: str) -> int | None:
        """Return entity_id for an exact alias match, or None.

        Args:
            alias: Lowercased, whitespace-collapsed alias to look up.

        Returns:
            entity_id if found, else None.
        """
        row = (
            self._conn()
            .execute("SELECT entity_id FROM entity_aliases WHERE alias = ?", (alias,))
            .fetchone()
        )
        return int(row[0]) if row else None

    def find_similar_entity(
        self,
        embedding: list[float],
        type_hint: str | None,
        threshold: float = 0.85,
    ) -> int | None:
        """Return entity_id of an existing entity with cosine similarity ≥ threshold, or None.

        Linear scan — suitable for tens of thousands of entities.

        Args:
            embedding: Query embedding vector.
            type_hint: Restrict scan to entities of this type; None to scan all.
            threshold: Minimum cosine similarity to consider a match.

        Returns:
            entity_id if a match is found, else None.
        """
        rows = (
            self._conn()
            .execute(
                "SELECT id, embedding FROM entities "
                "WHERE (? IS NULL OR type = ?) AND embedding IS NOT NULL",
                (type_hint, type_hint),
            )
            .fetchall()
        )
        best_id: int | None = None
        best_sim = threshold
        for eid, blob in rows:
            sim = _cosine(embedding, _blob_to_vec(blob))
            if sim > best_sim:
                best_id, best_sim = int(eid), sim
        return best_id

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
        from membox.normalize import normalize_name

        blob = _vec_to_blob(embedding) if embedding else None
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO entities(canonical_name, type, description, embedding) "
                "VALUES (?, ?, ?, ?)",
                (canonical, type_, description, blob),
            )
            eid = int(cur.lastrowid)  # type: ignore[arg-type]
            c.execute(
                "INSERT OR IGNORE INTO entity_aliases(alias, entity_id) VALUES (?, ?)",
                (normalize_name(canonical), eid),
            )
            return eid

    def add_alias(self, alias: str, entity_id: int) -> None:
        """Register an alias for an existing entity.

        Args:
            alias: Alias string to register.
            entity_id: Target entity.
        """
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO entity_aliases(alias, entity_id) VALUES (?, ?)",
                (alias, entity_id),
            )

    def update_entity_description(self, entity_id: int, new_desc: str) -> None:
        """Replace the entity description if new_desc is longer (keep-longer heuristic).

        Args:
            entity_id: Target entity.
            new_desc: Candidate new description.
        """
        if not new_desc:
            return
        with self._tx() as c:
            row = c.execute(
                "SELECT description FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                return
            if len(new_desc) > len(row[0] or ""):
                c.execute(
                    "UPDATE entities SET description = ? WHERE id = ?",
                    (new_desc, entity_id),
                )

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
        from membox.normalize import normalize_name

        alias = normalize_name(name)
        # Layer 1: exact alias match (fast path, no lock needed)
        eid = self.find_entity_by_alias(alias)
        if eid is not None:
            self.update_entity_description(eid, description)
            return eid

        # Layers 2+3 need the write lock to avoid duplicate inserts
        with self._write_lock:
            # Re-check under lock (another thread may have created it)
            eid = self.find_entity_by_alias(alias)
            if eid is not None:
                self.update_entity_description(eid, description)
                return eid

            # Layer 2: embedding similarity (full cascade enhanced in Phase 4)
            embedding: list[float] | None = None
            if embedder is not None:
                embedding = embedder.embed(name)
                eid = self.find_similar_entity(embedding, type_)
                if eid is not None:
                    self.add_alias(alias, eid)
                    self.update_entity_description(eid, description)
                    return eid

            # Layer 3: create new entity
            return self.create_entity(name, type_, description, embedding)

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
        with self._tx() as c:
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

    # ---- graph reads ----

    def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None:
        """Return (id, canonical_name, type, description) or None.

        Args:
            entity_id: Entity to look up.

        Returns:
            4-tuple or None if not found.
        """
        row = (
            self._conn()
            .execute(
                "SELECT id, canonical_name, type, description FROM entities WHERE id = ?",
                (entity_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return (int(row[0]), str(row[1]), str(row[2]), str(row[3]))

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
            self._conn()
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
            self._conn()
            .execute(
                f"SELECT re.relation_id, d.id, d.content "  # noqa: S608
                f"FROM relation_evidence re JOIN documents d ON d.id = re.doc_id "
                f"WHERE re.relation_id IN ({placeholders})",
                ids,
            )
            .fetchall()
        )
        return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]

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
        from membox.schema import Entity as EntityModel

        rows = (
            self._conn()
            .execute("SELECT id, canonical_name, type, created_at FROM entities ORDER BY id")
            .fetchall()
        )
        return [
            EntityModel(id=int(r[0]), name=str(r[1]), type=str(r[2]), created_at=str(r[3]))
            for r in rows
        ]

    def list_relations(self) -> list[Relation]:
        """Return all relations with source and target names resolved.

        Returns:
            List of Relation objects with source_name and target_name filled in.
        """
        from membox.schema import Relation as RelationModel

        rows = (
            self._conn()
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
