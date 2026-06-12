"""Memory-unit storage operations for lifecycle Phase C."""
# ruff: noqa: S608

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, TypedDict

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


class MemoryUnitHit(TypedDict):
    """One memory-unit search result row."""

    id: int
    project: str
    unit_type: str
    status: str
    title: str
    content: str
    context: str


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
            c.execute(
                """
                UPDATE memory_units
                SET status=?, superseded_by=COALESCE(?, superseded_by), updated_at=datetime('now')
                WHERE id=? AND status=?
                """,
                (to_status.value, superseded_by, unit_id, from_status),
            )
            if c.total_changes == 0:
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
