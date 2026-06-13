"""Schema migrations for the membox SQLite database, driven by ``PRAGMA user_version``.

The migration mechanism is an ordered list of ``(version, action)`` pairs.
Opening a store applies, inside a transaction per migration, every entry whose
version is greater than the database's current ``user_version`` and then bumps
``user_version`` to that entry's version.

Migration 0001 is the full v0.1 DDL written with ``CREATE TABLE IF NOT
EXISTS`` so that databases created before the migration mechanism existed
(``user_version == 0`` but tables already present) pass through idempotently.

Migration 0002 (M2 — Ingestion Hardening) adds document scoping metadata
columns (``project``, ``source_path``, ``section``, ``doc_date``, ``version``)
to ``documents`` via ``ALTER TABLE … ADD COLUMN`` and creates the ``meta``
table for embedding-model guard data.  Old databases at user_version=1 are
upgraded transparently; brand-new databases pass through both migrations in
sequence (migration 0001 creates the base tables, migration 0002 adds the new
columns on the same schema run).

Migration 0003 (M3 — Hybrid Retrieval) adds two structures:

1. **Relation embedding column** — ``relations.embedding BLOB`` stores the
   triple text (``"subject predicate object"``) embedding as packed IEEE-754
   float32 bytes (same encoding as ``entities.embedding``: ``struct.pack("Nf",
   ...)`` / ``struct.unpack``).  The embedding is computed once at ingest time
   and used for ``sim(t)`` in the composite scoring formula.  Storing directly
   on the relations table (rather than a sidecar table) avoids an extra JOIN
   and keeps the relation row self-contained.

2. **FTS5 virtual table (external-content)** — ``documents_fts`` is an
   external-content FTS5 table pointing at ``documents(content)``.  The
   external-content design was chosen over contentless because it avoids
   keeping a second copy of all text while still allowing snippet/highlight
   functions when needed.  Three ``AFTER INSERT / UPDATE / DELETE`` triggers on
   ``documents`` keep ``documents_fts`` in sync automatically.  A backfill
   ``INSERT INTO documents_fts`` seeds existing rows during the migration.

Migration 0004 (M6 — Asynchronous Ingestion Queue) creates the
``ingest_queue`` table: raw document text plus metadata captured at enqueue
time, drained by a short-lived worker process (spec §3.9).  The
``worker_lease`` single-worker guarantee reuses the existing ``meta`` table
(no schema change needed for the lease itself).

Migration 0005 adds ``documents_fts_trigram``, an additive CJK sidecar FTS5
index over ``documents(content)``.  The existing ``documents_fts`` table remains
the default unicode61 index for English/mixed queries; the sidecar is used only
by CJK-aware retrieval paths.

Migration 0006 (Lifecycle Phase B — History Trace Index) creates the trace
layer: ``history_sessions``, ``history_messages``, ``history_events``,
``history_import_state`` (per-source incremental import state), plus four FTS5
sidecars mirroring the documents pattern — unicode61 and trigram indexes for
both message ``text`` and event ``body``.  Messages and events use stable TEXT
ids (prefixed by ``source_kind``) as their public identity; an internal ``rid
INTEGER PRIMARY KEY`` alias exists only because external-content FTS5 requires
a rowid that stays stable across ``VACUUM`` (implicit rowids of text-PK tables
do not).  Stored ``text``/``body`` are secret-redacted, size-capped previews
(see ``core/triage.py`` and ``HistoryConfig.text_cap_bytes``); full payloads
stay in the upstream log, reachable via ``payload_locator``.

Migration 0007 (M4 — Supersession Semantics) adds the ``superseded_by``
self-referencing nullable FK column to ``relations``.  When a newer version of
the same source document asserts the same subject + predicate with a DIFFERENT
object, the old relation is marked ``superseded_by = <new_relation_id>``.
Evidence rows are never deleted.  A partial index ``idx_relations_active`` over
``(source_id) WHERE superseded_by IS NULL`` keeps active-relation lookups fast.
Retrieval excludes superseded relations by default; pass ``include_superseded``
to expose them for auditing.

Migration 0008 (Lifecycle Phase C — Triage + Memory Units) creates the
``history_triage`` audit table, ``memory_units`` plus source/label/status-log
tables, and unicode61 + trigram FTS5 sidecars over unit title/content/context.

Migration 0009 (M4 Part A2 — Agent-as-Provider Memory Units) adds three
nullable ``TEXT`` columns to ``memory_units``: ``why`` (the rationale behind
a decision/learning/procedure), ``how_to_apply`` (the procedure's concrete
recipe), and ``next_step`` (the immediate follow-up for a procedure/plan).
These are agent/LLM-extracted semantics, not deterministic checkpoint
fields, so they default to NULL on rows produced by the checkpoint path
(see ``core/lifecycle.py`` and ``_unit_from_trace`` in ``cli/commands/memory.py``).
"""
# ruff: noqa: S608

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


def _migrate_0002(conn: sqlite3.Connection) -> None:
    """Apply M2 ingestion-hardening schema changes.

    Adds nullable metadata columns to ``documents`` and creates the ``meta``
    table.  Uses ``ALTER TABLE … ADD COLUMN`` so existing data is preserved;
    new columns default to NULL.

    The ``version`` column records how many times a given ``source_path`` has
    been ingested — the first ingest is version 1, each re-ingest increments by
    1.  Rows created before migration 0002 default to NULL (unknown version).

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    new_columns = [
        ("project", "TEXT"),
        ("source_path", "TEXT"),
        ("section", "TEXT"),
        ("doc_date", "TEXT"),
        ("version", "INTEGER"),
    ]
    existing: set[str] = {
        row[1] for row in conn.execute("PRAGMA table_info(documents);").fetchall()
    }
    for col_name, col_type in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {col_type};")

    # Create meta table — stores embedding_model and embedding_dimensions once
    # at DB creation; mismatches on open raise a clear error (M3 guard logic).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        );
        """
    )

    # Index to speed up version lookups by source_path.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_source_path ON documents(source_path);")


def _migrate_0003(conn: sqlite3.Connection) -> None:
    """Apply M3 hybrid-retrieval schema changes.

    Adds:
    - ``relations.embedding BLOB``: packed float32 bytes for the triple embedding,
      computed at ingest time (one embedder call per relation write).  NULL until
      the relation is first written with an active embedder.
    - ``documents_fts``: external-content FTS5 virtual table over
      ``documents(content)`` with three sync triggers to stay up-to-date.

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    # 1. Add embedding column to relations (idempotent: skip if already present).
    rel_cols: set[str] = {
        row[1] for row in conn.execute("PRAGMA table_info(relations);").fetchall()
    }
    if "embedding" not in rel_cols:
        conn.execute("ALTER TABLE relations ADD COLUMN embedding BLOB;")

    # 2. Create FTS5 external-content virtual table.
    # content='documents' + content_rowid='id' tells FTS5 to read from
    # documents when it needs to re-rank / highlight; we manage index writes
    # ourselves via triggers below.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
        USING fts5(
            content,
            content='documents',
            content_rowid='id'
        );
        """
    )

    # 3. Sync triggers: keep documents_fts in lockstep with documents.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_ai
        AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_au
        AFTER UPDATE OF content ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            INSERT INTO documents_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_ad
        AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END;
        """
    )

    # 4. Backfill existing rows into the FTS index.
    conn.execute(
        """
        INSERT INTO documents_fts(rowid, content)
        SELECT id, content FROM documents;
        """
    )


_DDL_0004 = """
CREATE TABLE IF NOT EXISTS ingest_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    project     TEXT,
    source_path TEXT,
    doc_date    TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    retries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    enqueued_at TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON ingest_queue(status);
"""


def _migrate_0005(conn: sqlite3.Connection) -> None:
    """Apply the CJK trigram FTS sidecar schema changes.

    Adds an external-content FTS5 table using SQLite's built-in trigram
    tokenizer.  ``detail=none`` keeps the sidecar smaller; query code must emit
    3-character MATCH terms for this table.

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts_trigram
        USING fts5(
            content,
            content='documents',
            content_rowid='id',
            tokenize='trigram',
            detail=none
        );
        """
    )

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_tri_ai
        AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts_trigram(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_tri_au
        AFTER UPDATE OF content ON documents BEGIN
            INSERT INTO documents_fts_trigram(documents_fts_trigram, rowid, content)
                VALUES ('delete', old.id, old.content);
            INSERT INTO documents_fts_trigram(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docs_fts_tri_ad
        AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts_trigram(documents_fts_trigram, rowid, content)
                VALUES ('delete', old.id, old.content);
        END;
        """
    )

    conn.execute(
        """
        INSERT INTO documents_fts_trigram(rowid, content)
        SELECT id, content FROM documents;
        """
    )


def _create_history_fts(
    conn: sqlite3.Connection,
    fts_name: str,
    content_table: str,
    content_column: str,
    trigger_prefix: str,
    *,
    trigram: bool,
) -> None:
    """Create one external-content FTS5 sidecar with its three sync triggers.

    Mirrors the ``documents_fts`` / ``documents_fts_trigram`` pattern from
    migrations 0003/0005 for the history tables.  ``content_rowid`` is the
    explicit ``rid`` column.  No backfill is issued: migration 0006 creates
    the content tables in the same transaction, so they are empty.

    Args:
        conn: Open SQLite connection already inside a transaction.
        fts_name: Name of the FTS5 virtual table to create.
        content_table: External-content source table.
        content_column: Indexed text column on the source table.
        trigger_prefix: Unique prefix for the three trigger names.
        trigram: Use the trigram tokenizer with ``detail=none`` (CJK sidecar)
            instead of the default unicode61 tokenizer.
    """
    tokenize = ", tokenize='trigram', detail=none" if trigram else ""
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {fts_name}
        USING fts5(
            {content_column},
            content='{content_table}',
            content_rowid='rid'{tokenize}
        );
        """
    )
    # All interpolated names are module-internal constants, not user input.
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_ai
        AFTER INSERT ON {content_table} BEGIN
            INSERT INTO {fts_name}(rowid, {content_column})
                VALUES (new.rid, new.{content_column});
        END;
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_au
        AFTER UPDATE OF {content_column} ON {content_table} BEGIN
            INSERT INTO {fts_name}({fts_name}, rowid, {content_column})
                VALUES ('delete', old.rid, old.{content_column});
            INSERT INTO {fts_name}(rowid, {content_column})
                VALUES (new.rid, new.{content_column});
        END;
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_ad
        AFTER DELETE ON {content_table} BEGIN
            INSERT INTO {fts_name}({fts_name}, rowid, {content_column})
                VALUES ('delete', old.rid, old.{content_column});
        END;
        """
    )


def _migrate_0006(conn: sqlite3.Connection) -> None:
    """Apply the Lifecycle Phase B history-trace schema.

    Creates the trace tables (sessions, messages, events), the per-source
    incremental import-state table, query indexes, and the four FTS5 sidecars
    (unicode61 + trigram over message text and event body).

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history_sessions (
            id              TEXT PRIMARY KEY,
            external_id     TEXT NOT NULL DEFAULT '',
            project         TEXT NOT NULL DEFAULT '',
            title           TEXT NOT NULL DEFAULT '',
            started_at      TEXT,
            ended_at        TEXT,
            source_kind     TEXT NOT NULL,
            source_ref      TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history_messages (
            rid             INTEGER PRIMARY KEY,
            id              TEXT NOT NULL UNIQUE,
            session_id      TEXT NOT NULL REFERENCES history_sessions(id) ON DELETE CASCADE,
            project         TEXT NOT NULL DEFAULT '',
            external_id     TEXT NOT NULL,
            role            TEXT NOT NULL,
            agent_id        TEXT NOT NULL DEFAULT '',
            parent_id       TEXT,
            seq             INTEGER NOT NULL DEFAULT 0,
            text            TEXT NOT NULL DEFAULT '',
            text_truncated  INTEGER NOT NULL DEFAULT 0,
            payload_locator TEXT,
            created_at      TEXT,
            UNIQUE (session_id, external_id)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history_events (
            rid             INTEGER PRIMARY KEY,
            id              TEXT NOT NULL UNIQUE,
            session_id      TEXT NOT NULL REFERENCES history_sessions(id) ON DELETE CASCADE,
            project         TEXT NOT NULL DEFAULT '',
            message_id      TEXT REFERENCES history_messages(id) ON DELETE CASCADE,
            kind            TEXT NOT NULL,
            tool_name       TEXT,
            file_path       TEXT,
            ordinal         INTEGER NOT NULL DEFAULT 0,
            body            TEXT NOT NULL DEFAULT '',
            body_truncated  INTEGER NOT NULL DEFAULT 0,
            payload_locator TEXT,
            is_error        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history_import_state (
            source_ref      TEXT PRIMARY KEY,
            source_kind     TEXT NOT NULL,
            project         TEXT NOT NULL DEFAULT '',
            session_id      TEXT,
            mtime           REAL,
            size_bytes      INTEGER NOT NULL DEFAULT 0,
            offset_bytes    INTEGER NOT NULL DEFAULT 0,
            next_seq        INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_hmsg_session ON history_messages(session_id, seq);",
        "CREATE INDEX IF NOT EXISTS idx_hmsg_project ON history_messages(project, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_session ON history_events(session_id, ordinal);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_project ON history_events(project, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_kind ON history_events(project, kind, is_error);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_file ON history_events(file_path);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_project_file ON history_events(project, file_path);",
        "CREATE INDEX IF NOT EXISTS idx_hevt_message ON history_events(message_id);",
    ):
        conn.execute(ddl)

    _create_history_fts(
        conn, "history_messages_fts", "history_messages", "text", "hmsg_fts", trigram=False
    )
    _create_history_fts(
        conn, "history_messages_fts_trigram", "history_messages", "text", "hmsg_tri", trigram=True
    )
    _create_history_fts(
        conn, "history_events_fts", "history_events", "body", "hevt_fts", trigram=False
    )
    _create_history_fts(
        conn, "history_events_fts_trigram", "history_events", "body", "hevt_tri", trigram=True
    )


def _migrate_0007(conn: sqlite3.Connection) -> None:
    """Apply M4 supersession-semantics schema changes.

    Adds the ``superseded_by`` nullable self-referencing FK column to
    ``relations`` and a partial index on active (non-superseded) rows.
    Uses ``ALTER TABLE … ADD COLUMN`` so existing data is preserved; the new
    column defaults to NULL (not superseded).

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    rel_cols: set[str] = {
        row[1] for row in conn.execute("PRAGMA table_info(relations);").fetchall()
    }
    if "superseded_by" not in rel_cols:
        conn.execute(
            "ALTER TABLE relations ADD COLUMN superseded_by INTEGER REFERENCES relations(id);"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_active "
        "ON relations(source_id) WHERE superseded_by IS NULL;"
    )


def _migrate_0008(conn: sqlite3.Connection) -> None:
    """Apply Lifecycle Phase C triage and memory-unit schema changes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history_triage (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            project             TEXT NOT NULL DEFAULT '',
            trace_kind          TEXT NOT NULL,
            trace_id            TEXT NOT NULL,
            should_extract      INTEGER NOT NULL,
            unit_type           TEXT NOT NULL,
            importance_score    REAL NOT NULL DEFAULT 0,
            confidence_score    REAL NOT NULL DEFAULT 0,
            temporal_type       TEXT NOT NULL DEFAULT 'unknown',
            user_intent         TEXT NOT NULL DEFAULT 'auto',
            extraction_hint     TEXT NOT NULL DEFAULT '',
            reason              TEXT NOT NULL DEFAULT '',
            gate_version        TEXT NOT NULL,
            consumed_at         TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (trace_kind, trace_id, gate_version)
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_history_triage_pending
        ON history_triage(project, should_extract, consumed_at);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_units (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            project             TEXT NOT NULL DEFAULT '',
            unit_type           TEXT NOT NULL,
            status              TEXT NOT NULL,
            title               TEXT NOT NULL,
            content             TEXT NOT NULL,
            content_hash        TEXT NOT NULL,
            context             TEXT NOT NULL DEFAULT '',
            importance_score    REAL NOT NULL DEFAULT 0,
            confidence_score    REAL NOT NULL DEFAULT 0,
            temporal_type       TEXT NOT NULL DEFAULT 'unknown',
            valid_from          TEXT,
            valid_to            TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT,
            superseded_by       INTEGER REFERENCES memory_units(id),
            recall_count        INTEGER NOT NULL DEFAULT 0,
            last_recalled_at    TEXT,
            UNIQUE (project, unit_type, content_hash)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_unit_sources (
            unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
            source_kind         TEXT NOT NULL,
            source_ref          TEXT NOT NULL,
            source_message_id   TEXT NOT NULL DEFAULT '',
            quote               TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (unit_id, source_kind, source_ref, source_message_id)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_unit_labels (
            unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
            label               TEXT NOT NULL,
            PRIMARY KEY (unit_id, label)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_unit_status_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id             INTEGER NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
            from_status         TEXT,
            to_status           TEXT NOT NULL,
            command             TEXT NOT NULL,
            reason              TEXT NOT NULL DEFAULT '',
            source_ref          TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_memory_units_project_status "
        "ON memory_units(project, status);",
        "CREATE INDEX IF NOT EXISTS idx_memory_units_type ON memory_units(project, unit_type);",
        "CREATE INDEX IF NOT EXISTS idx_memory_unit_sources_lookup "
        "ON memory_unit_sources(source_kind, source_ref, source_message_id);",
    ):
        conn.execute(ddl)

    _create_memory_units_fts(conn, "memory_units_fts", "munit_fts", trigram=False)
    _create_memory_units_fts(conn, "memory_units_fts_trigram", "munit_tri", trigram=True)


def _create_memory_units_fts(
    conn: sqlite3.Connection,
    fts_name: str,
    trigger_prefix: str,
    *,
    trigram: bool,
) -> None:
    """Create one memory-unit FTS sidecar and sync triggers."""
    tokenize = ", tokenize='trigram', detail=none" if trigram else ""
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {fts_name}
        USING fts5(
            title,
            content,
            context,
            content='memory_units',
            content_rowid='id'{tokenize}
        );
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_ai
        AFTER INSERT ON memory_units BEGIN
            INSERT INTO {fts_name}(rowid, title, content, context)
            VALUES (new.id, new.title, new.content, new.context);
        END;
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_au
        AFTER UPDATE OF title, content, context ON memory_units BEGIN
            INSERT INTO {fts_name}({fts_name}, rowid, title, content, context)
            VALUES ('delete', old.id, old.title, old.content, old.context);
            INSERT INTO {fts_name}(rowid, title, content, context)
            VALUES (new.id, new.title, new.content, new.context);
        END;
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {trigger_prefix}_ad
        AFTER DELETE ON memory_units BEGIN
            INSERT INTO {fts_name}({fts_name}, rowid, title, content, context)
            VALUES ('delete', old.id, old.title, old.content, old.context);
        END;
        """
    )


def _migrate_0009(conn: sqlite3.Connection) -> None:
    """Apply M4 Part A2 agent-as-provider schema changes.

    Adds three nullable ``TEXT`` columns to ``memory_units`` — ``why``,
    ``how_to_apply``, ``next_step`` — that carry agent/LLM-extracted
    semantics.  Uses ``ALTER TABLE … ADD COLUMN`` so existing data is
    preserved; new columns default to NULL.  Idempotent: a column that
    already exists is skipped.

    Args:
        conn: Open SQLite connection already inside a transaction.
    """
    new_columns = [
        ("why", "TEXT"),
        ("how_to_apply", "TEXT"),
        ("next_step", "TEXT"),
    ]
    existing: set[str] = {
        row[1] for row in conn.execute("PRAGMA table_info(memory_units);").fetchall()
    }
    for col_name, col_type in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE memory_units ADD COLUMN {col_name} {col_type};")


MIGRATIONS: list[Migration] = [
    (1, _DDL_0001),
    (2, _migrate_0002),
    (3, _migrate_0003),
    (4, _DDL_0004),
    (5, _migrate_0005),
    (6, _migrate_0006),
    (7, _migrate_0007),
    (8, _migrate_0008),
    (9, _migrate_0009),
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
