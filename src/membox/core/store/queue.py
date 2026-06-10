"""Asynchronous ingestion queue operations (spec §3.9, M6).

Provides :class:`QueueOps`, a mixin for :class:`~membox.core.store.KnowledgeStore`
covering the ``ingest_queue`` table (created by migration 0004) and the
``worker_lease`` single-worker guarantee stored in the existing ``meta`` table.

The lease value is a JSON object ``{"pid": int, "hostname": str, "heartbeat":
ISO-8601 UTC timestamp}``.  A lease is *live* when its heartbeat is younger
than the TTL (default 60 s).  A spawner that observes a live lease does not
start a second worker; a worker that observes an expired lease takes over and
resets stale ``processing`` rows back to ``pending`` (crash recovery).
"""

from __future__ import annotations

import datetime
import json
import os
import socket
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from membox.core.store.connection import ConnectionManager


class QueueItem(TypedDict):
    """A claimed ingest-queue row as returned by ``claim_next_pending``."""

    id: int
    content: str
    project: str | None
    source_path: str | None
    doc_date: str | None
    retries: int


_LEASE_KEY = "worker_lease"

DEFAULT_LEASE_TTL: float = 60.0
"""Seconds a lease heartbeat stays valid before the lease counts as expired."""

DEFAULT_MAX_RETRIES: int = 3
"""Maximum ``retries`` value after which a failed row is permanently failed."""


def _utcnow() -> datetime.datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.datetime.now(tz=datetime.UTC)


class QueueOps:
    """Ingest-queue and worker-lease operations, mixed into ``KnowledgeStore``."""

    _cm: ConnectionManager

    # ---- enqueue / claim / complete ------------------------------------

    def enqueue_ingest(
        self,
        content: str,
        *,
        project: str | None = None,
        source_path: str | None = None,
        doc_date: str | None = None,
    ) -> int:
        """Insert a raw document into the ingest queue and return its queue id.

        This is the fast write-acceptance path: a single INSERT, no chunking
        and no LLM calls (those happen in the worker).

        Args:
            content: Raw document text.
            project: Repository / project name captured at enqueue time.
            source_path: Canonical file path captured at enqueue time.
            doc_date: ISO-8601 date string captured at enqueue time.

        Returns:
            Queue row id.
        """
        with self._cm.transaction() as c:
            cur = c.execute(
                """
                INSERT INTO ingest_queue (content, project, source_path, doc_date)
                VALUES (?, ?, ?, ?)
                """,
                (content, project, source_path, doc_date),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def claim_next_pending(self) -> QueueItem | None:
        """Atomically claim the oldest pending queue row (pending → processing).

        Returns:
            :class:`QueueItem` for the claimed row, or None when no pending
            rows remain.
        """
        with self._cm.transaction() as c:
            row = c.execute(
                """
                UPDATE ingest_queue
                SET status = 'processing', started_at = datetime('now')
                WHERE id = (
                    SELECT id FROM ingest_queue
                    WHERE status = 'pending'
                    ORDER BY id
                    LIMIT 1
                )
                RETURNING id, content, project, source_path, doc_date, retries
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "content": str(row[1]),
            "project": row[2],
            "source_path": row[3],
            "doc_date": row[4],
            "retries": int(row[5]),
        }

    def mark_done(self, queue_id: int) -> None:
        """Mark a claimed queue row as successfully processed.

        Args:
            queue_id: Queue row id previously returned by claim_next_pending.
        """
        with self._cm.transaction() as c:
            c.execute(
                """
                UPDATE ingest_queue
                SET status = 'done', error = NULL, finished_at = datetime('now')
                WHERE id = ?
                """,
                (queue_id,),
            )

    def mark_failed(self, queue_id: int, error: str) -> None:
        """Mark a claimed queue row as failed and increment its retry counter.

        Args:
            queue_id: Queue row id previously returned by claim_next_pending.
            error: Failure message, stored for inspection via ``membox queue``.
        """
        with self._cm.transaction() as c:
            c.execute(
                """
                UPDATE ingest_queue
                SET status = 'failed', error = ?, retries = retries + 1,
                    finished_at = datetime('now')
                WHERE id = ?
                """,
                (error, queue_id),
            )

    def retry_failed(self, max_retries: int = DEFAULT_MAX_RETRIES) -> int:
        """Reset retryable failed rows back to pending.

        Rows whose ``retries`` counter has reached *max_retries* stay failed
        permanently until manual intervention (spec §3.9).

        Args:
            max_retries: Retry ceiling; rows at or above it are not reset.

        Returns:
            Number of rows reset to pending.
        """
        with self._cm.transaction() as c:
            cur = c.execute(
                "UPDATE ingest_queue SET status = 'pending' "
                "WHERE status = 'failed' AND retries < ?",
                (max_retries,),
            )
            return int(cur.rowcount)

    # ---- observability --------------------------------------------------

    def queue_counts(self) -> dict[str, int]:
        """Return per-status row counts for the ingest queue.

        Returns:
            Mapping with keys ``pending``, ``processing``, ``done``, ``failed``
            (all present; zero when no rows have that status).
        """
        conn = self._cm.connection()
        counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM ingest_queue GROUP BY status;"
        ).fetchall():
            counts[str(status)] = int(count)
        return counts

    def pending_ingest_count(self) -> int:
        """Return the number of not-yet-materialized queue rows.

        Counts ``pending`` plus ``processing`` rows — the quantity surfaced in
        the query coverage footer so staleness is never silent (spec §3.9).

        Returns:
            Count of pending + processing rows.
        """
        conn = self._cm.connection()
        row = conn.execute(
            "SELECT COUNT(*) FROM ingest_queue WHERE status IN ('pending', 'processing');"
        ).fetchone()
        return int(row[0])

    def recent_failures(self, limit: int = 5) -> list[dict[str, object]]:
        """Return the most recent failed queue rows with their error messages.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            List of dicts with keys ``id``, ``source_path``, ``error``,
            ``retries``, ``finished_at``; most recent first.
        """
        conn = self._cm.connection()
        rows = conn.execute(
            """
            SELECT id, source_path, error, retries, finished_at
            FROM ingest_queue
            WHERE status = 'failed'
            ORDER BY finished_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": int(r[0]),
                "source_path": r[1],
                "error": r[2],
                "retries": int(r[3]),
                "finished_at": r[4],
            }
            for r in rows
        ]

    # ---- worker lease (single-worker guarantee) -------------------------

    def acquire_worker_lease(self, ttl: float = DEFAULT_LEASE_TTL) -> bool:
        """Try to take ownership of the worker lease.

        Succeeds when no lease exists, the existing lease has expired, or the
        existing lease is already owned by this process.  Taking over an
        expired lease also resets stale ``processing`` rows back to
        ``pending`` (crash recovery for a worker that died mid-item).

        Args:
            ttl: Lease time-to-live in seconds.

        Returns:
            True if this process now owns the lease; False when another
            worker's lease is still live.
        """
        with self._cm.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?;", (_LEASE_KEY,)).fetchone()
            if row is not None:
                lease = _parse_lease(str(row[0]))
                if lease is not None and _lease_is_live(lease, ttl) and not _lease_is_mine(lease):
                    return False
                # Expired or unparseable lease: take over and recover stale rows.
                c.execute(
                    "UPDATE ingest_queue SET status = 'pending', started_at = NULL "
                    "WHERE status = 'processing';"
                )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?);",
                (_LEASE_KEY, _render_lease()),
            )
            return True

    def refresh_worker_lease(self) -> None:
        """Refresh the lease heartbeat. Called by the worker after each item."""
        with self._cm.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?);",
                (_LEASE_KEY, _render_lease()),
            )

    def release_worker_lease(self) -> None:
        """Delete the lease if owned by this process. Called on worker exit."""
        with self._cm.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?;", (_LEASE_KEY,)).fetchone()
            if row is None:
                return
            lease = _parse_lease(str(row[0]))
            if lease is None or _lease_is_mine(lease):
                c.execute("DELETE FROM meta WHERE key = ?;", (_LEASE_KEY,))

    def worker_is_alive(self, ttl: float = DEFAULT_LEASE_TTL) -> bool:
        """Return True when a live worker lease exists (any process).

        Used by the enqueue path to decide whether spawning a worker is
        needed.  A lease owned by the current process also counts as alive.

        Args:
            ttl: Lease time-to-live in seconds.

        Returns:
            True when the recorded lease heartbeat is younger than *ttl*.
        """
        conn = self._cm.connection()
        row = conn.execute("SELECT value FROM meta WHERE key = ?;", (_LEASE_KEY,)).fetchone()
        if row is None:
            return False
        lease = _parse_lease(str(row[0]))
        return lease is not None and _lease_is_live(lease, ttl)


# ---------------------------------------------------------------------------
# Module-level lease helpers (also importable for tests)
# ---------------------------------------------------------------------------


def _render_lease() -> str:
    """Serialize a lease record for the current process as JSON."""
    return json.dumps(
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "heartbeat": _utcnow().isoformat(),
        }
    )


def _parse_lease(value: str) -> dict[str, object] | None:
    """Parse a lease JSON string; return None when malformed.

    Args:
        value: Raw ``meta.value`` string for the ``worker_lease`` key.

    Returns:
        Parsed lease dict, or None when the value is not a JSON object.
    """
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _lease_is_live(lease: dict[str, object], ttl: float) -> bool:
    """Return True when the lease heartbeat is younger than *ttl* seconds.

    A lease with a missing or unparseable heartbeat counts as expired.

    Args:
        lease: Parsed lease dict.
        ttl: Time-to-live in seconds.

    Returns:
        Liveness verdict.
    """
    raw = lease.get("heartbeat")
    if not isinstance(raw, str):
        return False
    try:
        heartbeat = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=datetime.UTC)
    return (_utcnow() - heartbeat).total_seconds() < ttl


def _lease_is_mine(lease: dict[str, object]) -> bool:
    """Return True when the lease is owned by the current process.

    Args:
        lease: Parsed lease dict.

    Returns:
        True when both pid and hostname match this process.
    """
    return lease.get("pid") == os.getpid() and lease.get("hostname") == socket.gethostname()
