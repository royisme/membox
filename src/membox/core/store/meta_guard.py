"""Embedding-model guard for the membox meta table.

On first embed-enabled store open the configured embedder's model name and
vector dimensionality are recorded in the ``meta`` table.  On subsequent opens
with an embedder, the recorded values are compared to the current embedder; a
mismatch raises :class:`EmbedderMismatchError` with a clear message telling the
user to re-embed.

When no embedder is configured the guard is a no-op: no read, no write.

The ``meta`` table schema (created by migration 0002)::

    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

Keys written by this module:

- ``embedding_model``   — model identifier string (may be empty for DummyEmbedder)
- ``embedding_dimensions`` — vector dimensionality as a decimal integer string
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


class EmbedderMismatchError(RuntimeError):
    """Raised when the configured embedder does not match the one recorded in the DB.

    The DB was populated with a different embedding model or dimensionality.
    All stored entity and relation embeddings are incompatible with the current
    embedder; the user must either switch back to the original embedder or
    re-embed the database from scratch.
    """


def check_embedder_guard(conn: sqlite3.Connection, model: str, dim: int) -> None:
    """Raise EmbedderMismatchError if the DB records a different embedder.

    Does nothing if the ``meta`` table has no embedding records yet (first
    embed-enabled open) or if the table does not exist.

    Args:
        conn: Open SQLite connection.
        model: Current embedder model identifier string.
        dim: Current embedder vector dimensionality.

    Raises:
        EmbedderMismatchError: If the recorded model or dimensions differ from
            the supplied values.
    """
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('embedding_model', 'embedding_dimensions');"
        ).fetchall()
    except Exception:
        return

    stored: dict[str, str] = {str(r[0]): str(r[1]) for r in rows}
    if not stored:
        # Nothing recorded yet — first embed-enabled open, let record_embedder_meta write.
        return

    stored_model = stored.get("embedding_model", "")
    stored_dim_str = stored.get("embedding_dimensions", "")

    mismatches: list[str] = []
    if stored_model != model:
        mismatches.append(f"embedding_model: stored={stored_model!r}, configured={model!r}")
    if stored_dim_str:
        try:
            stored_dim = int(stored_dim_str)
        except ValueError:
            stored_dim = -1
        if stored_dim != dim:
            mismatches.append(f"embedding_dimensions: stored={stored_dim}, configured={dim}")

    if mismatches:
        detail = "; ".join(mismatches)
        msg = (
            f"Embedder mismatch — the database was built with a different embedder "
            f"({detail}). Switch back to the original embedder or delete the database "
            f"and re-ingest to re-embed from scratch."
        )
        raise EmbedderMismatchError(msg)


def record_embedder_meta(conn: sqlite3.Connection, model: str, dim: int) -> None:
    """Write embedding_model and embedding_dimensions to meta if not yet recorded.

    Uses INSERT OR IGNORE so that already-recorded values are never overwritten
    (the guard in check_embedder_guard would have raised if they differed).

    Args:
        conn: Open SQLite connection.
        model: Embedder model identifier string.
        dim: Embedder vector dimensionality.
    """
    try:
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('embedding_model', ?);",
            (model,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('embedding_dimensions', ?);",
            (str(dim),),
        )
        conn.execute("COMMIT;")
    except Exception:
        import contextlib

        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK;")
