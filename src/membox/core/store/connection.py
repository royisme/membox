"""SQLite connection management: per-thread connections, PRAGMAs, transactions, locking."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


class ConnectionManager:
    """Owns per-thread SQLite connections and the process-wide write lock.

    Every connection is opened in autocommit mode with WAL journaling,
    foreign keys enabled, and NORMAL synchronous level. The ``write_lock``
    RLock serializes non-atomic critical sections (find-or-create) across
    threads in the same process.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        # Serializes the non-atomic "find-or-create entity" critical section.
        self.write_lock = threading.RLock()

    def connection(self) -> sqlite3.Connection:
        """Return the per-thread SQLite connection, creating it if needed.

        Returns:
            The calling thread's cached connection.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Context manager for explicit transactions with automatic rollback on error.

        Yields:
            The calling thread's connection, inside a ``BEGIN IMMEDIATE`` block.
        """
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        else:
            conn.execute("COMMIT;")

    def close(self) -> None:
        """Close the current thread's SQLite connection, if open.

        Connections are per-thread: this only closes the connection owned by
        the calling thread. A subsequent operation on this thread
        transparently reopens a fresh connection.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
