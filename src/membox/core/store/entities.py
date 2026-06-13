"""Entity persistence: CRUD, alias registry, and find-or-create deduplication."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from membox.core.store.vectors import (
    blob_to_vec,
    cosine,
    vec_to_blob,
)

if TYPE_CHECKING:
    from membox.core.store.connection import ConnectionManager
    from membox.model.schema import Entity
    from membox.services.embedding import Embedder

_blob_to_vec = blob_to_vec
_cosine = cosine
_vec_to_blob = vec_to_blob


class EntityOps:
    """Entity read/write operations, mixed into :class:`KnowledgeStore`."""

    _cm: ConnectionManager

    def find_entity_by_alias(self, alias: str) -> int | None:
        """Return entity_id for an exact alias match, or None.

        Args:
            alias: Lowercased, whitespace-collapsed alias to look up.

        Returns:
            entity_id if found, else None.
        """
        row = (
            self._cm.connection()
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
            self._cm.connection()
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
            vec = blob_to_vec(blob)
            # Rows written by an older embedder may have a different dimension;
            # skip them rather than letting one stale row break every lookup.
            if len(vec) != len(embedding):
                continue
            sim = cosine(embedding, vec)
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
        from membox.core.normalize import normalize_name

        blob = vec_to_blob(embedding) if embedding else None
        with self._cm.transaction() as c:
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
        with self._cm.transaction() as c:
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
        with self._cm.transaction() as c:
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
        *,
        threshold: float = 0.85,
    ) -> int:
        """Resolve name to an entity_id via alias → embedding → create.

        Resolution order:
          1. Exact alias match (cheap, deterministic).
          2. Embedding cosine ≥ ``threshold`` within same type (requires embedder).
          3. Create new entity.

        Safe for concurrent callers in the same process (RLock serializes the
        critical section) and across processes: if another process wins the
        race on the UNIQUE/PRIMARY KEY constraints, the resulting
        ``sqlite3.IntegrityError`` is caught and the winner's entity_id is
        re-resolved and returned instead of raising.

        Args:
            name: Entity surface form.
            type_: Entity type string.
            description: Short description for the entity.
            embedder: Optional embedder for fuzzy matching; None falls back to string-only.
            threshold: Minimum cosine similarity for the embedding match layer
                (``MemboxConfig.retrieval.disambiguation_threshold``).

        Returns:
            entity_id (existing or newly created).
        """
        from membox.core.normalize import normalize_name

        alias = normalize_name(name)
        # Layer 1: exact alias match (fast path, no lock needed)
        eid = self.find_entity_by_alias(alias)
        if eid is not None:
            self.update_entity_description(eid, description)
            return eid

        # Layers 2+3 need the write lock to avoid duplicate inserts
        with self._cm.write_lock:
            # Re-check under lock (another thread may have created it)
            eid = self.find_entity_by_alias(alias)
            if eid is not None:
                self.update_entity_description(eid, description)
                return eid

            # Layer 2: embedding similarity (full cascade enhanced in Phase 4)
            embedding: list[float] | None = None
            if embedder is not None:
                embedding = embedder.embed(name)
                eid = self.find_similar_entity(embedding, type_, threshold=threshold)
                if eid is not None:
                    self.add_alias(alias, eid)
                    # add_alias is INSERT OR IGNORE: a concurrent process may
                    # have bound this alias to a different entity first, in
                    # which case the existing mapping wins — defer to it.
                    winner = self.find_entity_by_alias(alias)
                    if winner is not None:
                        eid = winner
                    self.update_entity_description(eid, description)
                    return eid

            # Layer 3: create new entity
            try:
                return self.create_entity(name, type_, description, embedding)
            except sqlite3.IntegrityError:
                # Another process created the entity (or its alias) between our
                # re-check and the INSERT. Resolve to the winner's row.
                eid = self._resolve_race_winner(name, alias)
                if eid is None:  # pragma: no cover - defensive
                    raise
                self.update_entity_description(eid, description)
                return eid

    def _resolve_race_winner(self, name: str, alias: str) -> int | None:
        """Resolve the entity created by a concurrent process after a lost race.

        The alias table is keyed by the normalized name while the entities
        UNIQUE constraint is on the raw canonical name, so check both.

        Args:
            name: Raw canonical name as passed to find_or_create_entity.
            alias: Normalized alias for the name.

        Returns:
            entity_id of the winning row, or None if neither lookup matches.
        """
        eid = self.find_entity_by_alias(alias)
        if eid is not None:
            return eid
        row = (
            self._cm.connection()
            .execute("SELECT id FROM entities WHERE canonical_name = ?", (name,))
            .fetchone()
        )
        return int(row[0]) if row else None

    def get_entity(self, entity_id: int) -> tuple[int, str, str, str] | None:
        """Return (id, canonical_name, type, description) or None.

        Args:
            entity_id: Entity to look up.

        Returns:
            4-tuple or None if not found.
        """
        row = (
            self._cm.connection()
            .execute(
                "SELECT id, canonical_name, type, description FROM entities WHERE id = ?",
                (entity_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return (int(row[0]), str(row[1]), str(row[2]), str(row[3]))

    def list_entities(self) -> list[Entity]:
        """Return all entities in the graph.

        Returns:
            List of Entity objects ordered by id.
        """
        from membox.model.schema import Entity as EntityModel

        rows = (
            self._cm.connection()
            .execute("SELECT id, canonical_name, type, created_at FROM entities ORDER BY id")
            .fetchall()
        )
        return [
            EntityModel(id=int(r[0]), name=str(r[1]), type=str(r[2]), created_at=str(r[3]))
            for r in rows
        ]
