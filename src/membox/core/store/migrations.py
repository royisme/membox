"""Schema migrations for the membox SQLite database, driven by ``PRAGMA user_version``.

The migration mechanism is an ordered list of ``(version, action)`` pairs.
Opening a store applies, inside a transaction per migration, every entry whose
version is greater than the database's current ``user_version`` and then bumps
``user_version`` to that entry's version.

Migration 0001 is the full v0.1 DDL written with ``CREATE TABLE IF NOT
EXISTS`` so that databases created before the migration mechanism existed
(``user_version == 0`` but tables already present) pass through idempotently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    MigrationAction = str | Callable[[sqlite3.Connection], None]
    """A migration body: either a SQL script or a callable taking the connection."""

    Migration = tuple[int, "MigrationAction"]
    """One migration entry: (target user_version, action)."""


_DDL_0001 = """
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

MIGRATIONS: list[Migration] = [
    (1, _DDL_0001),
]


def latest_version(migrations: Sequence[Migration] | None = None) -> int:
    """Return the highest version in a migration list.

    Args:
        migrations: Migration list to inspect; defaults to :data:`MIGRATIONS`.

    Returns:
        Highest target user_version, or 0 for an empty list.
    """
    migs = MIGRATIONS if migrations is None else migrations
    return max((version for version, _ in migs), default=0)


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return the database's current ``PRAGMA user_version``.

    Args:
        conn: Open SQLite connection.

    Returns:
        Current schema version stored in the database header.
    """
    row = conn.execute("PRAGMA user_version;").fetchone()
    return int(row[0])


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> None:
    """Apply every pending migration to the database, in order.

    Each migration runs inside its own ``BEGIN IMMEDIATE`` transaction
    together with the ``user_version`` bump, so a failing migration leaves
    both the schema and the recorded version untouched.

    SQL actions are executed statement-by-statement (split on ``;``) rather
    than via ``executescript``, because ``executescript`` issues an implicit
    COMMIT that would break transactional rollback.

    Args:
        conn: Open SQLite connection (autocommit mode, as used by the store).
        migrations: Migration list to apply; defaults to :data:`MIGRATIONS`.

    Raises:
        ValueError: If migration versions are not strictly increasing.
    """
    migs = MIGRATIONS if migrations is None else migrations
    versions = [version for version, _ in migs]
    if versions != sorted(set(versions)):
        msg = f"Migration versions must be strictly increasing, got {versions}"
        raise ValueError(msg)

    current = get_user_version(conn)
    for version, action in migs:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE;")
        try:
            if callable(action):
                action(conn)
            else:
                for statement in action.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version:d};")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        conn.execute("COMMIT;")
        current = version
