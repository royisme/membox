"""History trace storage operations (lifecycle Phase B).

Provides :class:`HistoryOps`, a mixin for
:class:`~membox.core.store.KnowledgeStore` covering the ``history_sessions`` /
``history_messages`` / ``history_events`` tables (migration 0006) and the
per-source incremental import state.

Two store-boundary invariants are enforced here, not in importers:

- **Secret redaction** — :func:`~membox.core.triage.redact_secrets` runs over
  every message ``text`` and event ``body`` before insertion, so secrets are
  never persisted or FTS-indexed.  It is unconditional by design.
- **Preview cap** — stored text is truncated to ``text_cap_bytes`` (UTF-8
  safe) with the ``*_truncated`` flag set; the full payload stays in the
  upstream log, reachable via the row's identity-based ``payload_locator``.

Re-import is append-only and idempotent: rows upsert in place on their stable
keys, and rows whose upstream lines disappeared (compaction) are kept.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict

from membox.core.store.fts import (
    cjk_trigram_terms,
    contains_cjk,
    fts5_or_query,
    fts5_query_from_terms,
)
from membox.core.triage import redact_secrets

if TYPE_CHECKING:
    import sqlite3

    from membox.core.store.connection import ConnectionManager
    from membox.model.schema import (
        HistoryEventRecord,
        HistoryMessageRecord,
        HistorySessionRecord,
    )

DEFAULT_TEXT_CAP_BYTES = 16384
"""Fallback preview cap when no ``HistoryConfig`` value is supplied."""


class HistoryHit(TypedDict):
    """One history search result row."""

    kind: str
    id: str
    session_id: str
    project: str
    role_or_tool: str
    preview: str
    truncated: bool
    is_error: bool
    created_at: str | None


class ImportState(TypedDict):
    """Per-source incremental import state."""

    source_ref: str
    source_kind: str
    project: str
    session_id: str | None
    mtime: float | None
    size_bytes: int
    offset_bytes: int
    next_seq: int


def _cap_text(text: str, cap_bytes: int) -> tuple[str, bool]:
    """Truncate *text* to at most *cap_bytes* UTF-8 bytes on a char boundary.

    Args:
        text: Text to cap (already secret-redacted).
        cap_bytes: Maximum stored size in bytes.

    Returns:
        ``(stored_text, truncated)`` pair.
    """
    raw = text.encode("utf-8")
    if len(raw) <= cap_bytes:
        return text, False
    return raw[:cap_bytes].decode("utf-8", errors="ignore"), True


def _payload_locator(source_ref: str, external_id: str, ordinal: int | None = None) -> str:
    """Render the identity-based payload locator as compact JSON.

    The locator records *how to find* the full payload in the upstream
    source — never a byte offset, so upstream file rewrites do not break it.

    Args:
        source_ref: Upstream source reference (the session log path).
        external_id: Upstream record ID within that source.
        ordinal: Event ordinal within its parent message, when applicable.

    Returns:
        JSON string with keys ``source_ref``, ``external_id`` and, for
        events, ``ordinal``.
    """
    loc: dict[str, object] = {"source_ref": source_ref, "external_id": external_id}
    if ordinal is not None:
        loc["ordinal"] = ordinal
    return json.dumps(loc, ensure_ascii=False)


def _history_match(conn: sqlite3.Connection, base: str, query: str) -> tuple[str, str] | None:
    """Choose the FTS sidecar and MATCH expression for a history search.

    Dispatches CJK queries to the trigram sidecar (3-char terms) and
    everything else to the unicode61 sidecar, reusing the shared FTS
    sanitizers — raw user strings never reach MATCH.

    Args:
        conn: Open SQLite connection.
        base: Content table name (``history_messages`` or ``history_events``).
        query: Raw user query string.

    Returns:
        ``(fts_table, match_expr)`` or None when the query sanitizes to
        nothing.
    """
    if contains_cjk(query):
        terms = cjk_trigram_terms(query)
        if terms:
            return f"{base}_fts_trigram", fts5_query_from_terms(terms)
    match_expr = fts5_or_query(query)
    if match_expr == '""':
        return None
    return f"{base}_fts", match_expr


class HistoryOps:
    """History trace operations, mixed into ``KnowledgeStore``."""

    _cm: ConnectionManager

    # ---- import (upsert) ------------------------------------------------

    def upsert_history_session(self, session: HistorySessionRecord) -> None:
        """Insert or update one history session row.

        Args:
            session: Normalized session record from an importer.
        """
        with self._cm.transaction() as c:
            c.execute(
                """
                INSERT INTO history_sessions
                    (id, external_id, project, title, started_at, ended_at,
                     source_kind, source_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project    = excluded.project,
                    title      = excluded.title,
                    started_at = COALESCE(excluded.started_at, started_at),
                    ended_at   = COALESCE(excluded.ended_at, ended_at),
                    source_ref = excluded.source_ref
                """,
                (
                    session.id,
                    session.external_id,
                    session.project,
                    session.title,
                    session.started_at,
                    session.ended_at,
                    session.source_kind.value,
                    session.source_ref,
                ),
            )

    def upsert_history_messages(
        self,
        session: HistorySessionRecord,
        messages: list[HistoryMessageRecord],
        *,
        text_cap_bytes: int = DEFAULT_TEXT_CAP_BYTES,
    ) -> int:
        """Redact, cap, and upsert message rows for one session.

        ``project`` is denormalized from the parent session (importers cannot
        violate the project invariant).  Existing rows whose upstream lines
        vanished are left untouched — trace is append-only.

        Args:
            session: Parent session (must already be upserted).
            messages: Normalized messages with full, uncapped ``text``.
            text_cap_bytes: Preview cap from ``HistoryConfig.text_cap_bytes``.

        Returns:
            Number of rows written (inserted or updated).
        """
        written = 0
        with self._cm.transaction() as c:
            for msg in messages:
                stored, truncated = _cap_text(redact_secrets(msg.text), text_cap_bytes)
                locator = _payload_locator(session.source_ref, msg.external_id)
                c.execute(
                    """
                    INSERT INTO history_messages
                        (id, session_id, project, external_id, role, agent_id,
                         parent_id, seq, text, text_truncated, payload_locator,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, external_id) DO UPDATE SET
                        role            = excluded.role,
                        agent_id        = excluded.agent_id,
                        parent_id       = excluded.parent_id,
                        seq             = excluded.seq,
                        text            = excluded.text,
                        text_truncated  = excluded.text_truncated,
                        payload_locator = excluded.payload_locator,
                        created_at      = COALESCE(excluded.created_at, created_at)
                    """,
                    (
                        msg.id,
                        session.id,
                        session.project,
                        msg.external_id,
                        msg.role,
                        msg.agent_id,
                        msg.parent_id,
                        msg.seq,
                        stored,
                        int(truncated),
                        locator,
                        msg.created_at,
                    ),
                )
                written += 1
        return written

    def upsert_history_events(
        self,
        session: HistorySessionRecord,
        events: list[HistoryEventRecord],
        *,
        text_cap_bytes: int = DEFAULT_TEXT_CAP_BYTES,
    ) -> int:
        """Redact, cap, and upsert event rows for one session.

        Args:
            session: Parent session (must already be upserted).
            events: Normalized events with full, uncapped ``body``.
            text_cap_bytes: Preview cap from ``HistoryConfig.text_cap_bytes``.

        Returns:
            Number of rows written (inserted or updated).
        """
        written = 0
        with self._cm.transaction() as c:
            for evt in events:
                stored, truncated = _cap_text(redact_secrets(evt.body), text_cap_bytes)
                locator = _payload_locator(
                    session.source_ref, evt.message_external_id or evt.anchor, evt.ordinal
                )
                c.execute(
                    """
                    INSERT INTO history_events
                        (id, session_id, project, message_id, kind, tool_name,
                         file_path, ordinal, body, body_truncated,
                         payload_locator, is_error, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        tool_name       = excluded.tool_name,
                        file_path       = excluded.file_path,
                        ordinal         = excluded.ordinal,
                        body            = excluded.body,
                        body_truncated  = excluded.body_truncated,
                        payload_locator = excluded.payload_locator,
                        is_error        = excluded.is_error,
                        created_at      = COALESCE(excluded.created_at, created_at)
                    """,
                    (
                        evt.id,
                        session.id,
                        session.project,
                        evt.message_id,
                        evt.kind.value,
                        evt.tool_name,
                        evt.file_path,
                        evt.ordinal,
                        stored,
                        int(truncated),
                        locator,
                        int(evt.is_error),
                        evt.created_at,
                    ),
                )
                written += 1
        return written

    # ---- incremental import state ---------------------------------------

    def get_import_state(self, source_ref: str) -> ImportState | None:
        """Return the stored incremental state for one source file, if any.

        Args:
            source_ref: Upstream source reference (the session log path).

        Returns:
            Stored state, or None when this source was never imported.
        """
        row = (
            self._cm.connection()
            .execute(
                """
            SELECT source_ref, source_kind, project, session_id, mtime,
                   size_bytes, offset_bytes, next_seq
            FROM history_import_state WHERE source_ref = ?
            """,
                (source_ref,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ImportState(
            source_ref=str(row[0]),
            source_kind=str(row[1]),
            project=str(row[2]),
            session_id=row[3] if row[3] is None else str(row[3]),
            mtime=row[4] if row[4] is None else float(row[4]),
            size_bytes=int(row[5]),
            offset_bytes=int(row[6]),
            next_seq=int(row[7]),
        )

    def set_import_state(self, state: ImportState) -> None:
        """Insert or update the incremental state for one source file.

        Args:
            state: New state to persist (replaces any previous row).
        """
        with self._cm.transaction() as c:
            c.execute(
                """
                INSERT INTO history_import_state
                    (source_ref, source_kind, project, session_id, mtime,
                     size_bytes, offset_bytes, next_seq, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_ref) DO UPDATE SET
                    source_kind  = excluded.source_kind,
                    project      = excluded.project,
                    session_id   = excluded.session_id,
                    mtime        = excluded.mtime,
                    size_bytes   = excluded.size_bytes,
                    offset_bytes = excluded.offset_bytes,
                    next_seq     = excluded.next_seq,
                    updated_at   = excluded.updated_at
                """,
                (
                    state["source_ref"],
                    state["source_kind"],
                    state["project"],
                    state["session_id"],
                    state["mtime"],
                    state["size_bytes"],
                    state["offset_bytes"],
                    state["next_seq"],
                ),
            )

    # ---- lookups ---------------------------------------------------------

    def get_history_session(self, session_id: str) -> dict[str, object] | None:
        """Return one session row as a dict, or None.

        Args:
            session_id: Stable session ID.
        """
        row = (
            self._cm.connection()
            .execute(
                """
            SELECT id, external_id, project, title, started_at, ended_at,
                   source_kind, source_ref
            FROM history_sessions WHERE id = ?
            """,
                (session_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        keys = (
            "id",
            "external_id",
            "project",
            "title",
            "started_at",
            "ended_at",
            "source_kind",
            "source_ref",
        )
        return dict(zip(keys, row, strict=True))

    def get_history_record(self, record_id: str) -> dict[str, object] | None:
        """Return one message or event row by stable ID, or None.

        Used by ``history fetch`` and ``history around`` to resolve an ID
        without knowing whether it names a message or an event.

        Args:
            record_id: Stable message or event ID.

        Returns:
            Row dict including a ``trace_kind`` key (``message`` / ``event``),
            or None when the ID matches nothing.
        """
        conn = self._cm.connection()
        row = conn.execute(
            """
            SELECT id, session_id, project, external_id, role, seq, text,
                   text_truncated, payload_locator, created_at
            FROM history_messages WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
        if row is not None:
            keys = (
                "id",
                "session_id",
                "project",
                "external_id",
                "role",
                "seq",
                "text",
                "text_truncated",
                "payload_locator",
                "created_at",
            )
            out = dict(zip(keys, row, strict=True))
            out["trace_kind"] = "message"
            return out
        row = conn.execute(
            """
            SELECT id, session_id, project, message_id, kind, tool_name,
                   file_path, ordinal, body, body_truncated, payload_locator,
                   is_error, created_at
            FROM history_events WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
        if row is not None:
            event_keys = (
                "id",
                "session_id",
                "project",
                "message_id",
                "kind",
                "tool_name",
                "file_path",
                "ordinal",
                "body",
                "body_truncated",
                "payload_locator",
                "is_error",
                "created_at",
            )
            out = dict(zip(event_keys, row, strict=True))
            out["trace_kind"] = "event"
            return out
        return None

    # ---- search ----------------------------------------------------------

    def search_history(
        self,
        query: str,
        *,
        project: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        tool: str | None = None,
        file_path: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[HistoryHit]:
        """Full-text search over message previews and event bodies.

        Punctuation and CJK are handled by the shared FTS sanitizers; raw
        user strings never reach a MATCH expression.  Rows with NULL
        ``created_at`` are excluded by ``since`` (the upstream lacked a
        timestamp), matching the design's explicit-gap policy.

        Args:
            query: Raw user query.
            project: Project filter; None means all projects (the CLI only
                passes None under an explicit ``--all-projects``).
            session_id: Restrict to one session.
            kind: Event-kind filter (``tool_call``, ``tool_result``, …).
                ``tool_error`` is an alias for ``tool_result`` with
                ``is_error=1``.  Any kind filter skips the message pool.
            tool: Tool-name filter (events only).
            file_path: Exact-path or directory-prefix filter on event ``file_path``.
            since: ISO-8601 lower bound on ``created_at``.
            limit: Maximum hits returned across both pools.

        Returns:
            Hits ordered by BM25 rank (messages and events interleaved by
            their per-pool rank order).
        """
        conn = self._cm.connection()
        hits: list[tuple[float, HistoryHit]] = []
        is_error_only = kind == "tool_error"
        kind_filter = "tool_result" if is_error_only else kind
        events_only = bool(kind or tool or file_path)

        if not events_only:
            dispatch = _history_match(conn, "history_messages", query)
            if dispatch is not None:
                fts, match = dispatch
                sql = (
                    f"SELECT m.id, m.session_id, m.project, m.role, m.text, "  # noqa: S608
                    f"m.text_truncated, m.created_at, bm25({fts}) "
                    f"FROM {fts} f JOIN history_messages m ON m.rid = f.rowid "
                    f"WHERE f.{fts} MATCH ?"
                )
                params: list[object] = [match]
                sql, params = _append_filters(
                    sql, params, "m", project=project, session_id=session_id, since=since
                )
                sql += f" ORDER BY bm25({fts}) LIMIT ?"
                params.append(limit)
                hits.extend(
                    (
                        float(r[7]),
                        HistoryHit(
                            kind="message",
                            id=str(r[0]),
                            session_id=str(r[1]),
                            project=str(r[2]),
                            role_or_tool=str(r[3]),
                            preview=str(r[4]),
                            truncated=bool(r[5]),
                            is_error=False,
                            created_at=r[6] if r[6] is None else str(r[6]),
                        ),
                    )
                    for r in conn.execute(sql, params).fetchall()
                )

        dispatch = _history_match(conn, "history_events", query)
        if dispatch is not None:
            fts, match = dispatch
            sql = (
                f"SELECT e.id, e.session_id, e.project, e.kind, e.tool_name, "  # noqa: S608
                f"e.body, e.body_truncated, e.is_error, e.created_at, bm25({fts}) "
                f"FROM {fts} f JOIN history_events e ON e.rid = f.rowid "
                f"WHERE f.{fts} MATCH ?"
            )
            params = [match]
            sql, params = _append_filters(
                sql, params, "e", project=project, session_id=session_id, since=since
            )
            if kind_filter:
                sql += " AND e.kind = ?"
                params.append(kind_filter)
            if is_error_only:
                sql += " AND e.is_error = 1"
            if tool:
                sql += " AND e.tool_name = ?"
                params.append(tool)
            if file_path:
                clause, values = _file_path_filter("e", file_path)
                sql += f" AND {clause}"
                params.extend(values)
            sql += f" ORDER BY bm25({fts}) LIMIT ?"
            params.append(limit)
            for r in conn.execute(sql, params).fetchall():
                label = str(r[4]) if r[4] is not None else str(r[3])
                hits.append(
                    (
                        float(r[9]),
                        HistoryHit(
                            kind="event",
                            id=str(r[0]),
                            session_id=str(r[1]),
                            project=str(r[2]),
                            role_or_tool=label,
                            preview=str(r[5]),
                            truncated=bool(r[6]),
                            is_error=bool(r[7]),
                            created_at=r[8] if r[8] is None else str(r[8]),
                        ),
                    )
                )

        hits.sort(key=lambda pair: pair[0])
        return [hit for _, hit in hits[:limit]]

    def history_around(
        self, message_id: str, *, radius: int = 3, project: str | None = None
    ) -> list[dict[str, object]]:
        """Return the messages surrounding one message in its session.

        Args:
            message_id: Stable message ID at the center of the window.
            radius: Messages to include on each side (by ``seq`` order).
            project: Optional project scope guard.  When set, rows from other
                projects are treated as unknown.

        Returns:
            Message dicts ordered by ``seq``; empty when the ID is unknown.
        """
        conn = self._cm.connection()
        center = conn.execute(
            "SELECT session_id, seq, project FROM history_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if center is None:
            return []
        if project is not None and str(center[2]) != project:
            return []
        session_id, seq = str(center[0]), int(center[1])
        rows = conn.execute(
            """
            SELECT id, role, seq, text, text_truncated, created_at
            FROM history_messages
            WHERE session_id = ? AND seq BETWEEN ? AND ?
            ORDER BY seq
            """,
            (session_id, seq - radius, seq + radius),
        ).fetchall()
        keys = ("id", "role", "seq", "text", "text_truncated", "created_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def history_file(
        self, file_path: str, *, project: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        """Return events that touched a file or directory prefix, newest first.

        Args:
            file_path: Exact file path or directory prefix.
            project: Project filter; None means all projects.
            limit: Maximum rows.

        Returns:
            Event dicts ordered by ``created_at`` descending (NULLs last).
        """
        sql = (
            "SELECT id, session_id, project, kind, tool_name, file_path, "
            "body, is_error, created_at FROM history_events WHERE "
        )
        clause, params = _file_path_filter("", file_path)
        sql += clause
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY created_at IS NULL, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._cm.connection().execute(sql, params).fetchall()
        keys = (
            "id",
            "session_id",
            "project",
            "kind",
            "tool_name",
            "file_path",
            "body",
            "is_error",
            "created_at",
        )
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def history_failures(
        self, *, project: str | None = None, since: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        """Return failed tool events, newest first.

        Args:
            project: Project filter; None means all projects.
            since: ISO-8601 lower bound on ``created_at`` (NULLs excluded).
            limit: Maximum rows.

        Returns:
            Event dicts for rows with ``is_error = 1``.
        """
        sql = (
            "SELECT id, session_id, project, kind, tool_name, file_path, "
            "body, created_at FROM history_events WHERE is_error = 1"
        )
        params: list[object] = []
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at IS NULL, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._cm.connection().execute(sql, params).fetchall()
        keys = (
            "id",
            "session_id",
            "project",
            "kind",
            "tool_name",
            "file_path",
            "body",
            "created_at",
        )
        return [dict(zip(keys, r, strict=True)) for r in rows]


def _append_filters(
    sql: str,
    params: list[object],
    alias: str,
    *,
    project: str | None,
    session_id: str | None,
    since: str | None,
) -> tuple[str, list[object]]:
    """Append the shared project/session/since WHERE clauses to a search query.

    Args:
        sql: Query so far (must already contain a WHERE clause).
        params: Bind parameters so far (mutated in place and returned).
        alias: Table alias used in the query.
        project: Project filter or None.
        session_id: Session filter or None.
        since: ISO-8601 ``created_at`` lower bound or None (NULLs excluded).

    Returns:
        Updated ``(sql, params)``.
    """
    if project is not None:
        sql += f" AND {alias}.project = ?"
        params.append(project)
    if session_id is not None:
        sql += f" AND {alias}.session_id = ?"
        params.append(session_id)
    if since is not None:
        sql += f" AND {alias}.created_at >= ?"
        params.append(since)
    return sql, params


def _file_path_filter(alias: str, file_path: str) -> tuple[str, list[object]]:
    """Return an index-friendly exact-or-directory-prefix file-path predicate."""
    column = f"{alias}.file_path" if alias else "file_path"
    base = file_path.rstrip("/") or file_path
    prefix = f"{base}/%"
    return f"({column} = ? OR {column} LIKE ?)", [base, prefix]
