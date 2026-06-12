"""Memory-unit storage operations for lifecycle Phase C."""
# ruff: noqa: S608

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from membox.core.consolidate import evolved_confidence
from membox.core.store.retrieval import (
    _cjk_trigram_terms,
    _contains_cjk,
    _fts5_or_query,
    _fts5_query_from_terms,
)
from membox.model.schema import (
    MEMORY_LABELS,
    HistoryTriageRecord,
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    MemoryUnitType,
)

if TYPE_CHECKING:
    import sqlite3

    from membox.core.store.connection import ConnectionManager

_TERMINAL_DEDUP_STATUSES = {MemoryUnitStatus.RETRACTED.value}
MEMORY_CRYSTAL_BOOST = 1.5
"""Owner-calibrated score multiplier for crystals in query-side memory recall."""

MEMORY_RECENCY_DAYS = 30.0
"""Owner-calibrated half-life-like window for updated_at recency scoring."""


class MemoryUnitHit(TypedDict):
    """One memory-unit search result row."""

    id: int
    project: str
    unit_type: str
    status: str
    title: str
    content: str
    context: str


class MemoryQueryHit(MemoryUnitHit):
    """One ranked memory hit eligible for query-side memory fusion."""

    importance_score: float
    confidence_score: float
    updated_at: str | None
    recall_count: int
    last_recalled_at: str | None
    relevance_score: float
    stored_score: float
    recency_score: float
    score: float


class TraceForTriage(TypedDict):
    """One history trace row eligible for triage."""

    trace_kind: str
    trace_id: str
    project: str
    role: str
    text: str
    created_at: str | None


class MemoryUnitOps:
    """Memory triage and unit operations, mixed into ``KnowledgeStore``."""

    _cm: ConnectionManager

    def trace_rows_for_triage(
        self,
        *,
        project: str,
        since: str | None = None,
        limit: int = 100,
    ) -> list[TraceForTriage]:
        """Return history messages/events not yet triaged for the current gate."""
        clauses = ["project=?"]
        params_one: list[object] = [project]
        if since is not None:
            clauses.append("created_at>=?")
            params_one.append(since)
        where = " AND ".join(clauses)
        params = [*params_one, *params_one, limit]
        rows = (
            self._cm.connection()
            .execute(
                f"""
            SELECT 'message' AS trace_kind, id, project, role, text, created_at
            FROM history_messages
            WHERE {where}
            UNION ALL
            SELECT 'event' AS trace_kind, id, project, COALESCE(tool_name, kind), body, created_at
            FROM history_events
            WHERE {where}
            ORDER BY created_at
            LIMIT ?
            """,
                params,
            )
            .fetchall()
        )
        return [
            {
                "trace_kind": str(row[0]),
                "trace_id": str(row[1]),
                "project": str(row[2]),
                "role": str(row[3]),
                "text": str(row[4]),
                "created_at": None if row[5] is None else str(row[5]),
            }
            for row in rows
        ]

    def get_trace_text(self, trace_kind: str, trace_id: str) -> TraceForTriage | None:
        """Fetch one history message or event for extraction."""
        if trace_kind == "message":
            row = (
                self._cm.connection()
                .execute(
                    """
                SELECT 'message', id, project, role, text, created_at
                FROM history_messages WHERE id=?
                """,
                    (trace_id,),
                )
                .fetchone()
            )
        elif trace_kind == "event":
            row = (
                self._cm.connection()
                .execute(
                    """
                SELECT 'event', id, project, COALESCE(tool_name, kind), body, created_at
                FROM history_events WHERE id=?
                """,
                    (trace_id,),
                )
                .fetchone()
            )
        else:
            return None
        if row is None:
            return None
        return {
            "trace_kind": str(row[0]),
            "trace_id": str(row[1]),
            "project": str(row[2]),
            "role": str(row[3]),
            "text": str(row[4]),
            "created_at": None if row[5] is None else str(row[5]),
        }

    def upsert_history_triage(self, record: HistoryTriageRecord) -> int:
        """Insert or update one triage decision.

        Args:
            record: Triage decision to persist.

        Returns:
            Row id of the inserted or updated triage record.
        """
        with self._cm.transaction() as c:
            c.execute(
                """
                INSERT INTO history_triage
                    (project, trace_kind, trace_id, should_extract, unit_type,
                     importance_score, confidence_score, temporal_type,
                     user_intent, extraction_hint, reason, gate_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_kind, trace_id, gate_version) DO UPDATE SET
                    project = excluded.project,
                    should_extract = excluded.should_extract,
                    unit_type = excluded.unit_type,
                    importance_score = excluded.importance_score,
                    confidence_score = excluded.confidence_score,
                    temporal_type = excluded.temporal_type,
                    user_intent = excluded.user_intent,
                    extraction_hint = excluded.extraction_hint,
                    reason = excluded.reason
                """,
                (
                    record.project,
                    record.trace_kind.value,
                    record.trace_id,
                    int(record.should_extract),
                    record.unit_type.value,
                    record.importance_score,
                    record.confidence_score,
                    record.temporal_type.value,
                    record.user_intent.value,
                    record.extraction_hint,
                    record.reason,
                    record.gate_version,
                ),
            )
            row = c.execute(
                """
                SELECT id FROM history_triage
                WHERE trace_kind=? AND trace_id=? AND gate_version=?
                """,
                (record.trace_kind.value, record.trace_id, record.gate_version),
            ).fetchone()
        return int(row[0])

    def pending_triage_rows(
        self,
        *,
        project: str,
        gate_version: str,
        limit: int = 100,
    ) -> list[HistoryTriageRecord]:
        """Return newest-version pending triage rows selected for extraction."""
        rows = (
            self._cm.connection()
            .execute(
                """
            SELECT id, project, trace_kind, trace_id, should_extract, unit_type,
                   importance_score, confidence_score, temporal_type, user_intent,
                   extraction_hint, reason, gate_version, consumed_at, created_at
            FROM history_triage
            WHERE project=? AND gate_version=? AND should_extract=1 AND consumed_at IS NULL
            ORDER BY id
            LIMIT ?
            """,
                (project, gate_version, limit),
            )
            .fetchall()
        )
        return [_triage_from_row(row) for row in rows]

    def mark_triage_consumed(self, triage_ids: list[int]) -> None:
        """Set ``consumed_at`` for triage rows."""
        if not triage_ids:
            return
        placeholders = ",".join("?" * len(triage_ids))
        with self._cm.transaction() as c:
            c.execute(
                f"UPDATE history_triage SET consumed_at=datetime('now') WHERE id IN ({placeholders})",
                triage_ids,
            )

    def create_memory_unit(self, unit: MemoryUnitRecord, *, command: str = "memory extract") -> int:
        """Insert a memory unit plus labels, sources, status log, and FTS rows.

        Args:
            unit: Unit data to write. Must include at least one source.
            command: Command name recorded in the status log.

        Returns:
            New unit id, or an existing non-retracted unit id when a source
            identity is already covered.

        Raises:
            ValueError: If type, status, label, source kind, or provenance is invalid.
        """
        _validate_memory_unit(unit)
        existing = self.find_unit_covering_sources(unit.sources)
        if existing is not None:
            return existing
        content_hash = memory_unit_content_hash(unit)
        with self._cm.transaction() as c:
            c.execute(
                """
                INSERT INTO memory_units
                    (project, unit_type, status, title, content, content_hash, context,
                     importance_score, confidence_score, temporal_type, valid_from, valid_to,
                     superseded_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(project, unit_type, content_hash) DO NOTHING
                """,
                (
                    unit.project,
                    unit.unit_type.value,
                    unit.status.value,
                    unit.title,
                    unit.content,
                    content_hash,
                    unit.context,
                    unit.importance_score,
                    unit.confidence_score,
                    unit.temporal_type.value,
                    unit.valid_from,
                    unit.valid_to,
                    unit.superseded_by,
                ),
            )
            unit_id = _lookup_unit_id(c, unit.project, unit.unit_type.value, content_hash)
            for label in unit.labels:
                c.execute(
                    "INSERT OR IGNORE INTO memory_unit_labels(unit_id, label) VALUES (?, ?)",
                    (unit_id, label),
                )
            for source in unit.sources:
                c.execute(
                    """
                    INSERT OR IGNORE INTO memory_unit_sources
                        (unit_id, source_kind, source_ref, source_message_id, quote)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        unit_id,
                        source.source_kind.value,
                        source.source_ref,
                        source.source_message_id,
                        source.quote,
                    ),
                )
            c.execute(
                """
                INSERT INTO memory_unit_status_log
                    (unit_id, from_status, to_status, command, reason, source_ref)
                VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    unit.status.value,
                    command,
                    "created",
                    unit.sources[0].source_ref,
                ),
            )
        return unit_id

    def get_memory_unit(self, unit_id: int) -> MemoryUnitRecord | None:
        """Return one memory unit with labels and sources."""
        conn = self._cm.connection()
        row = conn.execute(
            """
            SELECT id, project, unit_type, status, title, content, context,
                   importance_score, confidence_score, temporal_type,
                   valid_from, valid_to, superseded_by, created_at, updated_at,
                   recall_count, last_recalled_at
            FROM memory_units WHERE id=?
            """,
            (unit_id,),
        ).fetchone()
        if row is None:
            return None
        return _unit_from_row(conn, row)

    def list_memory_units(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryUnitRecord]:
        """List memory units newest first."""
        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("project=?")
            params.append(project)
        if status is not None:
            MemoryUnitStatus(status)
            clauses.append("status=?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        conn = self._cm.connection()
        rows = conn.execute(
            f"""
            SELECT id, project, unit_type, status, title, content, context,
                   importance_score, confidence_score, temporal_type,
                   valid_from, valid_to, superseded_by, created_at, updated_at,
                   recall_count, last_recalled_at
            FROM memory_units
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_unit_from_row(conn, row) for row in rows]

    def transition_memory_unit(
        self,
        unit_id: int,
        to_status: MemoryUnitStatus,
        *,
        command: str,
        reason: str = "",
        source_ref: str = "",
        superseded_by: int | None = None,
    ) -> bool:
        """Transition a unit status and append a status-log row."""
        with self._cm.transaction() as c:
            row = c.execute("SELECT status FROM memory_units WHERE id=?", (unit_id,)).fetchone()
            if row is None:
                return False
            from_status = str(row[0])
            cursor = c.execute(
                """
                UPDATE memory_units
                SET status=?, superseded_by=COALESCE(?, superseded_by), updated_at=datetime('now')
                WHERE id=? AND status=?
                """,
                (to_status.value, superseded_by, unit_id, from_status),
            )
            if cursor.rowcount == 0:
                return False
            c.execute(
                """
                INSERT INTO memory_unit_status_log
                    (unit_id, from_status, to_status, command, reason, source_ref)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (unit_id, from_status, to_status.value, command, reason, source_ref),
            )
        return True

    def list_units_for_consolidation(
        self,
        *,
        project: str,
        since: str | None = None,
        limit: int = 500,
    ) -> list[MemoryUnitRecord]:
        """Return units eligible for Phase D consolidation planning."""
        clauses = ["project=?"]
        params: list[object] = [project]
        if since is not None:
            clauses.append("COALESCE(updated_at, created_at)>=?")
            params.append(since)
        params.append(limit)
        conn = self._cm.connection()
        rows = conn.execute(
            f"""
            SELECT id, project, unit_type, status, title, content, context,
                   importance_score, confidence_score, temporal_type,
                   valid_from, valid_to, superseded_by, created_at, updated_at,
                   recall_count, last_recalled_at
            FROM memory_units
            WHERE {" AND ".join(clauses)}
              AND status NOT IN ('archived', 'superseded', 'retracted')
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_unit_from_row(conn, row) for row in rows]

    def list_units_for_distill(
        self,
        *,
        project: str,
        since: str | None = None,
        limit: int = 500,
    ) -> list[MemoryUnitRecord]:
        """Return workflow-like memory units eligible for Phase F distillation."""
        clauses = [
            "project=?",
            "unit_type IN (?, ?)",
            "status IN (?, ?, ?)",
            "superseded_by IS NULL",
        ]
        params: list[object] = [
            project,
            MemoryUnitType.PROCEDURE.value,
            MemoryUnitType.LEARNING.value,
            MemoryUnitStatus.ACTIVE_UNIT.value,
            MemoryUnitStatus.CRYSTAL_CANDIDATE.value,
            MemoryUnitStatus.CRYSTAL.value,
        ]
        if since is not None:
            clauses.append("COALESCE(updated_at, created_at)>=?")
            params.append(since)
        params.append(limit)
        conn = self._cm.connection()
        rows = conn.execute(
            f"""
            SELECT id, project, unit_type, status, title, content, context,
                   importance_score, confidence_score, temporal_type,
                   valid_from, valid_to, superseded_by, created_at, updated_at,
                   recall_count, last_recalled_at
            FROM memory_units
            WHERE {" AND ".join(clauses)}
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_unit_from_row(conn, row) for row in rows]

    def count_independent_sources(self, unit_id: int) -> int:
        """Count distinct independent sources for crystal promotion."""
        return len(self._independent_source_keys(unit_id))

    def count_independent_sources_for_units(self, unit_ids: list[int]) -> dict[int, int]:
        """Count independent sources for many memory units in one query."""
        if not unit_ids:
            return {}
        unique_ids = sorted(set(unit_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = (
            self._cm.connection()
            .execute(
                f"""
            SELECT mus.unit_id, mus.source_kind, mus.source_ref,
                   hm.session_id AS message_session_id,
                   he.session_id AS event_session_id
            FROM memory_unit_sources mus
            LEFT JOIN history_messages hm
              ON mus.source_kind=? AND hm.id=mus.source_ref
            LEFT JOIN history_events he
              ON mus.source_kind=? AND he.id=mus.source_ref
            WHERE mus.unit_id IN ({placeholders})
            """,
                [
                    MemorySourceKind.HISTORY_MESSAGE.value,
                    MemorySourceKind.HISTORY_EVENT.value,
                    *unique_ids,
                ],
            )
            .fetchall()
        )
        keys_by_unit = {unit_id: set[str]() for unit_id in unique_ids}
        for row in rows:
            unit_id = int(str(row[0]))
            source_kind = str(row[1])
            source_ref = str(row[2])
            if source_kind == MemorySourceKind.HISTORY_MESSAGE.value:
                session = str(row[3]) if row[3] is not None else _trace_session_key(source_ref)
                keys_by_unit[unit_id].add(f"session:{session}")
            elif source_kind == MemorySourceKind.HISTORY_EVENT.value:
                session = str(row[4]) if row[4] is not None else _trace_session_key(source_ref)
                keys_by_unit[unit_id].add(f"session:{session}")
            else:
                keys_by_unit[unit_id].add(f"{source_kind}:{source_ref}")
        return {unit_id: len(keys) for unit_id, keys in keys_by_unit.items()}

    def count_independent_sources_for_unit_group(self, unit_ids: list[int]) -> int:
        """Count distinct independent sources across a group of memory units."""
        if not unit_ids:
            return 0
        unique_ids = sorted(set(unit_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = (
            self._cm.connection()
            .execute(
                f"""
            SELECT mus.source_kind, mus.source_ref,
                   hm.session_id AS message_session_id,
                   he.session_id AS event_session_id
            FROM memory_unit_sources mus
            LEFT JOIN history_messages hm
              ON mus.source_kind=? AND hm.id=mus.source_ref
            LEFT JOIN history_events he
              ON mus.source_kind=? AND he.id=mus.source_ref
            WHERE mus.unit_id IN ({placeholders})
            """,
                [
                    MemorySourceKind.HISTORY_MESSAGE.value,
                    MemorySourceKind.HISTORY_EVENT.value,
                    *unique_ids,
                ],
            )
            .fetchall()
        )
        keys: set[str] = set()
        for row in rows:
            source_kind = str(row[0])
            source_ref = str(row[1])
            if source_kind == MemorySourceKind.HISTORY_MESSAGE.value:
                session = str(row[2]) if row[2] is not None else _trace_session_key(source_ref)
                keys.add(f"session:{session}")
            elif source_kind == MemorySourceKind.HISTORY_EVENT.value:
                session = str(row[3]) if row[3] is not None else _trace_session_key(source_ref)
                keys.add(f"session:{session}")
            else:
                keys.add(f"{source_kind}:{source_ref}")
        return len(keys)

    def attach_memory_unit_source(
        self,
        unit_id: int,
        source: MemoryUnitSource,
        *,
        command: str = "memory consolidate",
        reason: str = "attached supporting source",
    ) -> bool:
        """Attach a source and evolve confidence for new independent support."""
        if not source.source_ref:
            msg = "memory unit sources require source_ref"
            raise ValueError(msg)
        with self._cm.transaction() as c:
            row = c.execute(
                "SELECT confidence_score FROM memory_units WHERE id=?",
                (unit_id,),
            ).fetchone()
            if row is None:
                return False
            before = self._independent_source_keys(unit_id, conn=c)
            c.execute(
                """
                INSERT OR IGNORE INTO memory_unit_sources
                    (unit_id, source_kind, source_ref, source_message_id, quote)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    source.source_kind.value,
                    source.source_ref,
                    source.source_message_id,
                    source.quote,
                ),
            )
            after = self._independent_source_keys(unit_id, conn=c)
            gained = len(after - before)
            if gained > 0:
                c.execute(
                    """
                    UPDATE memory_units
                    SET confidence_score=?, updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (evolved_confidence(float(row[0]), gained), unit_id),
                )
                c.execute(
                    """
                    INSERT INTO memory_unit_status_log
                        (unit_id, from_status, to_status, command, reason, source_ref)
                    SELECT id, status, status, ?, ?, ? FROM memory_units WHERE id=?
                    """,
                    (command, reason, source.source_ref, unit_id),
                )
        return True

    def restore_memory_unit(self, unit_id: int, *, reason: str = "") -> bool:
        """Restore an archived unit to its pre-archive status from the status log."""
        conn = self._cm.connection()
        row = conn.execute(
            """
            SELECT from_status FROM memory_unit_status_log
            WHERE unit_id=? AND to_status='archived'
            ORDER BY id DESC LIMIT 1
            """,
            (unit_id,),
        ).fetchone()
        prior = row[0] if row and row[0] else MemoryUnitStatus.ACTIVE_UNIT.value
        return self.transition_memory_unit(
            unit_id,
            MemoryUnitStatus(prior),
            command="memory restore",
            reason=reason,
        )

    def find_unit_covering_sources(self, sources: list[MemoryUnitSource]) -> int | None:
        """Find a non-retracted unit already covering any source identity."""
        if not sources:
            return None
        conn = self._cm.connection()
        for source in sources:
            row = conn.execute(
                """
                SELECT mus.unit_id
                FROM memory_unit_sources mus
                JOIN memory_units mu ON mu.id=mus.unit_id
                WHERE mus.source_kind=? AND mus.source_ref=? AND mus.source_message_id=?
                  AND mu.status NOT IN ('retracted')
                ORDER BY mus.unit_id DESC
                LIMIT 1
                """,
                (source.source_kind.value, source.source_ref, source.source_message_id),
            ).fetchone()
            if row is not None:
                return int(row[0])
        return None

    def search_memory_units(
        self,
        query: str,
        *,
        project: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[MemoryUnitHit]:
        """Search memory units through sanitized unicode/trigram FTS."""
        if not query.strip() or limit <= 0:
            return []
        conn = self._cm.connection()
        if _contains_cjk(query):
            terms = _cjk_trigram_terms(query)
            if terms:
                fts_name = "memory_units_fts_trigram"
                match_expr = _fts5_query_from_terms(terms)
            else:
                fts_name = "memory_units_fts"
                match_expr = _fts5_or_query(query)
        else:
            fts_name = "memory_units_fts"
            match_expr = _fts5_or_query(query)
        if match_expr == '""':
            return []
        clauses = [f"{fts_name} MATCH ?"]
        params: list[object] = [match_expr]
        if project is not None:
            clauses.append("mu.project=?")
            params.append(project)
        if status is not None:
            MemoryUnitStatus(status)
            clauses.append("mu.status=?")
            params.append(status)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT mu.id, mu.project, mu.unit_type, mu.status, mu.title, mu.content, mu.context
            FROM {fts_name}
            JOIN memory_units mu ON mu.id={fts_name}.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY bm25({fts_name})
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": int(row[0]),
                "project": str(row[1]),
                "unit_type": str(row[2]),
                "status": str(row[3]),
                "title": str(row[4]),
                "content": str(row[5]),
                "context": str(row[6]),
            }
            for row in rows
        ]

    def search_memory_units_for_query(
        self,
        project: str | None,
        query_terms: str,
        *,
        limit: int = 20,
    ) -> list[MemoryQueryHit]:
        """Return ranked active units/crystals for opt-in query memory fusion.

        The read path is deterministic and offline: FTS relevance is multiplied
        by stored importance/confidence, updated_at recency, and a crystal boost.
        Superseded, retracted, archived, and raw unit_candidate rows are excluded.
        """
        if not query_terms.strip() or limit <= 0:
            return []
        conn = self._cm.connection()
        fts_name, match_expr = _memory_fts_table_and_query(query_terms)
        if match_expr == '""':
            return []
        clauses = [
            f"{fts_name} MATCH ?",
            "mu.status IN (?, ?, ?)",
            "mu.superseded_by IS NULL",
        ]
        params: list[object] = [
            match_expr,
            MemoryUnitStatus.ACTIVE_UNIT.value,
            MemoryUnitStatus.CRYSTAL_CANDIDATE.value,
            MemoryUnitStatus.CRYSTAL.value,
        ]
        if project is not None:
            clauses.append("mu.project=?")
            params.append(project)
        params.append(limit * 4)
        rows = conn.execute(
            f"""
            SELECT mu.id, mu.project, mu.unit_type, mu.status, mu.title, mu.content,
                   mu.context, mu.importance_score, mu.confidence_score, mu.updated_at,
                   mu.recall_count, mu.last_recalled_at, bm25({fts_name}) AS rank
            FROM {fts_name}
            JOIN memory_units mu ON mu.id={fts_name}.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()
        if not rows:
            return []
        ranks = [float(row[12]) for row in rows]
        best = min(ranks)
        worst = max(ranks)
        hits: list[MemoryQueryHit] = []
        for row in rows:
            status = str(row[3])
            relevance = _normalized_bm25(float(row[12]), best, worst)
            importance = float(str(row[7]))
            confidence = float(str(row[8]))
            stored = max(0.0, importance) * max(0.0, confidence)
            recency = _memory_recency_factor(None if row[9] is None else str(row[9]))
            tier = MEMORY_CRYSTAL_BOOST if status == MemoryUnitStatus.CRYSTAL.value else 1.0
            score = relevance * stored * recency * tier
            hits.append(
                {
                    "id": int(row[0]),
                    "project": str(row[1]),
                    "unit_type": str(row[2]),
                    "status": status,
                    "title": str(row[4]),
                    "content": str(row[5]),
                    "context": str(row[6]),
                    "importance_score": importance,
                    "confidence_score": confidence,
                    "updated_at": None if row[9] is None else str(row[9]),
                    "recall_count": int(str(row[10])),
                    "last_recalled_at": None if row[11] is None else str(row[11]),
                    "relevance_score": relevance,
                    "stored_score": stored,
                    "recency_score": recency,
                    "score": score,
                }
            )
        return sorted(
            hits,
            key=lambda hit: (
                -hit["score"],
                hit["status"] != MemoryUnitStatus.CRYSTAL.value,
                hit["id"],
            ),
        )[:limit]

    def mark_memory_units_recalled(self, unit_ids: list[int]) -> None:
        """Bump recall bookkeeping for admitted query-side memories."""
        if not unit_ids:
            return
        unique_ids = sorted(set(unit_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self._cm.transaction() as c:
            c.execute(
                f"""
                UPDATE memory_units
                SET recall_count=recall_count + 1,
                    last_recalled_at=datetime('now'),
                    updated_at=updated_at
                WHERE id IN ({placeholders})
                """,
                unique_ids,
            )

    def acquire_lifecycle_lease(self, project: str) -> bool:
        """Acquire the lifecycle lease for one project."""
        from membox.core.store.queue import (
            _lease_is_live,
            _lease_is_mine,
            _parse_lease,
            _render_lease,
        )

        key = f"lifecycle_lease:{project}"
        payload = _render_lease()
        with self._cm.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?;", (key,)).fetchone()
            if row:
                lease = _parse_lease(str(row[0]))
                if lease is not None and _lease_is_live(lease, 30.0) and not _lease_is_mine(lease):
                    return False
            c.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, payload),
            )
        return True

    def release_lifecycle_lease(self, project: str) -> None:
        """Release the lifecycle lease for one project when owned by this process."""
        from membox.core.store.queue import _lease_is_mine, _parse_lease

        key = f"lifecycle_lease:{project}"
        with self._cm.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?;", (key,)).fetchone()
            lease = _parse_lease(str(row[0])) if row else None
            if lease is None or _lease_is_mine(lease):
                c.execute("DELETE FROM meta WHERE key=?;", (key,))

    def _independent_source_keys(
        self,
        unit_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> set[str]:
        """Return source keys according to the Phase D independence rule."""
        c = self._cm.connection() if conn is None else conn
        rows = c.execute(
            """
            SELECT source_kind, source_ref
            FROM memory_unit_sources
            WHERE unit_id=?
            """,
            (unit_id,),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            source_kind = str(row[0])
            source_ref = str(row[1])
            if source_kind == MemorySourceKind.HISTORY_MESSAGE.value:
                session = c.execute(
                    "SELECT session_id FROM history_messages WHERE id=?",
                    (source_ref,),
                ).fetchone()
                keys.add(f"session:{session[0] if session else _trace_session_key(source_ref)}")
            elif source_kind == MemorySourceKind.HISTORY_EVENT.value:
                session = c.execute(
                    "SELECT session_id FROM history_events WHERE id=?",
                    (source_ref,),
                ).fetchone()
                keys.add(f"session:{session[0] if session else _trace_session_key(source_ref)}")
            else:
                keys.add(f"{source_kind}:{source_ref}")
        return keys


def memory_unit_content_hash(unit: MemoryUnitRecord) -> str:
    """Return the normalized content hash for a memory unit."""
    normalized = "\n".join(
        [
            unit.unit_type.value,
            unit.title.strip().casefold(),
            unit.content.strip().casefold(),
            unit.context.strip().casefold(),
            unit.project.strip().casefold(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_memory_unit(unit: MemoryUnitRecord) -> None:
    """Validate unit enum fields, labels, and provenance."""
    MemoryUnitType(unit.unit_type.value)
    MemoryUnitStatus(unit.status.value)
    if not unit.sources:
        msg = "memory units require at least one source"
        raise ValueError(msg)
    unknown = sorted(set(unit.labels) - MEMORY_LABELS)
    if unknown:
        msg = f"unknown memory labels: {', '.join(unknown)}"
        raise ValueError(msg)
    for source in unit.sources:
        MemorySourceKind(source.source_kind.value)
        if not source.source_ref:
            msg = "memory unit sources require source_ref"
            raise ValueError(msg)


def _lookup_unit_id(
    conn: sqlite3.Connection,
    project: str,
    unit_type: str,
    content_hash: str,
) -> int:
    """Look up a unit id by unique content hash."""
    row = conn.execute(
        """
        SELECT id FROM memory_units
        WHERE project=? AND unit_type=? AND content_hash=?
        """,
        (project, unit_type, content_hash),
    ).fetchone()
    if row is None:
        msg = "memory unit insert did not produce a row"
        raise RuntimeError(msg)
    return int(row[0])


def _trace_session_key(source_ref: str) -> str:
    """Best-effort session key from a stable history message/event id."""
    for marker in (":msg:", ":evt:"):
        if marker in source_ref:
            return source_ref.split(marker, maxsplit=1)[0]
    return source_ref


def _memory_fts_table_and_query(query: str) -> tuple[str, str]:
    """Choose memory-unit FTS sidecar and MATCH expression for a query."""
    if _contains_cjk(query):
        terms = _cjk_trigram_terms(query)
        if terms:
            return "memory_units_fts_trigram", _fts5_query_from_terms(terms)
    return "memory_units_fts", _fts5_or_query(query)


def _normalized_bm25(rank: float, best: float, worst: float) -> float:
    """Normalize SQLite FTS5 bm25 rank to 0..1 where 1 is most relevant."""
    if best == worst:
        return 1.0
    return max(0.0, min(1.0, (worst - rank) / (worst - best)))


def _memory_recency_factor(updated_at: str | None) -> float:
    """Return a bounded recency factor based on age since updated_at."""
    if updated_at is None:
        return 1.0
    parsed = _parse_sqlite_datetime(updated_at)
    if parsed is None:
        return 1.0
    age_days = max(0.0, (datetime.now(UTC) - parsed).total_seconds() / 86_400)
    return 1.0 / (1.0 + (age_days / MEMORY_RECENCY_DAYS))


def _parse_sqlite_datetime(value: str) -> datetime | None:
    """Parse SQLite datetime strings as UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _triage_from_row(row: tuple[object, ...]) -> HistoryTriageRecord:
    """Hydrate a triage record from SQLite."""
    from membox.model.schema import MemoryTemporalType, MemoryUserIntent, TraceKind

    return HistoryTriageRecord(
        id=int(str(row[0])),
        project=str(row[1]),
        trace_kind=TraceKind(str(row[2])),
        trace_id=str(row[3]),
        should_extract=bool(row[4]),
        unit_type=MemoryUnitType(str(row[5])),
        importance_score=float(str(row[6])),
        confidence_score=float(str(row[7])),
        temporal_type=MemoryTemporalType(str(row[8])),
        user_intent=MemoryUserIntent(str(row[9])),
        extraction_hint=str(row[10]),
        reason=str(row[11]),
        gate_version=str(row[12]),
        consumed_at=None if row[13] is None else str(row[13]),
        created_at=str(row[14]),
    )


def _unit_from_row(conn: sqlite3.Connection, row: tuple[object, ...]) -> MemoryUnitRecord:
    """Hydrate a memory unit row with labels and sources."""
    from membox.model.schema import MemoryTemporalType

    unit_id = int(str(row[0]))
    labels = [
        str(label_row[0])
        for label_row in conn.execute(
            "SELECT label FROM memory_unit_labels WHERE unit_id=? ORDER BY label",
            (unit_id,),
        ).fetchall()
    ]
    sources = [
        MemoryUnitSource(
            source_kind=MemorySourceKind(str(source_row[0])),
            source_ref=str(source_row[1]),
            source_message_id=str(source_row[2]),
            quote=str(source_row[3]),
        )
        for source_row in conn.execute(
            """
            SELECT source_kind, source_ref, source_message_id, quote
            FROM memory_unit_sources WHERE unit_id=?
            ORDER BY source_kind, source_ref, source_message_id
            """,
            (unit_id,),
        ).fetchall()
    ]
    return MemoryUnitRecord(
        id=unit_id,
        project=str(row[1]),
        unit_type=MemoryUnitType(str(row[2])),
        status=MemoryUnitStatus(str(row[3])),
        title=str(row[4]),
        content=str(row[5]),
        context=str(row[6]),
        importance_score=float(str(row[7])),
        confidence_score=float(str(row[8])),
        temporal_type=MemoryTemporalType(str(row[9])),
        valid_from=None if row[10] is None else str(row[10]),
        valid_to=None if row[11] is None else str(row[11]),
        superseded_by=None if row[12] is None else int(str(row[12])),
        created_at=str(row[13]),
        updated_at=None if row[14] is None else str(row[14]),
        recall_count=int(str(row[15])),
        last_recalled_at=None if row[16] is None else str(row[16]),
        labels=labels,
        sources=sources,
    )
