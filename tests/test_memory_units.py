"""Lifecycle Phase C memory-unit storage tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from membox.cli import app
from membox.core.store import KnowledgeStore
from membox.core.store.migrations import MIGRATIONS, apply_migrations, get_user_version
from membox.core.triage import GATE_VERSION
from membox.model.schema import (
    HistoryMessageRecord,
    HistorySessionRecord,
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    MemoryUnitType,
    SourceKind,
)

runner = CliRunner()


def _unit() -> MemoryUnitRecord:
    """Return a minimal valid memory unit for tests."""
    return MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.DECISION,
        status=MemoryUnitStatus.ACTIVE_UNIT,
        title="Use migration 8 for Phase C",
        content="Phase C memory-unit schema is migration 8.",
        context="Owner pinned migration numbering.",
        importance_score=0.8,
        confidence_score=0.75,
        labels=["architecture", "storage"],
        sources=[
            MemoryUnitSource(
                source_kind=MemorySourceKind.HISTORY_MESSAGE,
                source_ref="msg-1",
                source_message_id="msg-1",
                quote="migration 8",
            )
        ],
    )


def test_fresh_db_reaches_migration_8(tmp_path: Path) -> None:
    """Fresh stores apply the Phase C migration."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    assert get_user_version(store._conn()) == 8
    tables = {
        row[0]
        for row in store._conn()
        .execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table');")
        .fetchall()
    }
    assert {
        "history_triage",
        "memory_units",
        "memory_unit_sources",
        "memory_unit_labels",
        "memory_unit_status_log",
        "memory_units_fts",
        "memory_units_fts_trigram",
    }.issubset(tables)


def test_v7_db_upgrades_to_memory_units(tmp_path: Path) -> None:
    """A database at user_version 7 upgrades to 8 without reset."""
    db_path = str(tmp_path / "v7.db")
    conn = sqlite3.connect(db_path)
    for version, action in MIGRATIONS[:7]:
        apply_migrations(conn, migrations=[(version, action)])
    assert get_user_version(conn) == 7
    conn.close()

    store = KnowledgeStore(db_path)
    assert get_user_version(store._conn()) == 8


def test_create_memory_unit_rejects_unknown_label(tmp_path: Path) -> None:
    """Labels are closed and validated in Python."""
    store = KnowledgeStore(str(tmp_path / "units.db"))
    unit = _unit()
    unit.labels.append("unknown")

    with pytest.raises(ValueError, match="unknown memory labels"):
        store.create_memory_unit(unit)


def test_create_memory_unit_requires_source(tmp_path: Path) -> None:
    """Every unit needs at least one source."""
    store = KnowledgeStore(str(tmp_path / "units.db"))
    unit = _unit()
    unit.sources = []

    with pytest.raises(ValueError, match="at least one source"):
        store.create_memory_unit(unit)


def test_source_identity_dedup_is_idempotent(tmp_path: Path) -> None:
    """Re-running extraction over the same source returns the existing unit."""
    store = KnowledgeStore(str(tmp_path / "units.db"))
    first = store.create_memory_unit(_unit())
    second = store.create_memory_unit(_unit())

    assert second == first
    rows = store._conn().execute("SELECT COUNT(*) FROM memory_units;").fetchone()
    assert rows[0] == 1


def test_status_transition_logs(tmp_path: Path) -> None:
    """Status transitions update the row and append an audit log."""
    store = KnowledgeStore(str(tmp_path / "units.db"))
    unit_id = store.create_memory_unit(_unit())

    assert store.transition_memory_unit(
        unit_id,
        MemoryUnitStatus.RETRACTED,
        command="memory retract",
        reason="bad source",
    )
    unit = store.get_memory_unit(unit_id)
    assert unit is not None
    assert unit.status == MemoryUnitStatus.RETRACTED
    row = (
        store._conn()
        .execute(
            """
        SELECT from_status, to_status, command, reason
        FROM memory_unit_status_log
        WHERE unit_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
            (unit_id,),
        )
        .fetchone()
    )
    assert row == ("active_unit", "retracted", "memory retract", "bad source")


def test_memory_unit_fts_search(tmp_path: Path) -> None:
    """Memory unit search uses the FTS sidecar."""
    store = KnowledgeStore(str(tmp_path / "units.db"))
    unit_id = store.create_memory_unit(_unit())

    hits = store.search_memory_units("migration Phase C", project="membox")

    assert [hit["id"] for hit in hits] == [unit_id]


def test_query_memory_pool_filters_and_ranks(tmp_path: Path) -> None:
    """Query-side memory pool returns ranked active units/crystals only."""
    store = KnowledgeStore(str(tmp_path / "units.db"))

    def make_unit(
        title: str,
        status: MemoryUnitStatus,
        project: str = "membox",
    ) -> MemoryUnitRecord:
        unit = _unit()
        unit.project = project
        unit.title = title
        unit.content = "Migration Phase E query memory fusion uses ranked memory recall."
        unit.status = status
        unit.sources = [
            MemoryUnitSource(
                source_kind=MemorySourceKind.HISTORY_MESSAGE,
                source_ref=f"msg-{title}",
                source_message_id=f"msg-{title}",
                quote="Phase E query memory",
            )
        ]
        return unit

    active_id = store.create_memory_unit(make_unit("Active memory", MemoryUnitStatus.ACTIVE_UNIT))
    crystal_id = store.create_memory_unit(make_unit("Crystal memory", MemoryUnitStatus.CRYSTAL))
    store.create_memory_unit(make_unit("Candidate memory", MemoryUnitStatus.CRYSTAL_CANDIDATE))
    store.create_memory_unit(make_unit("Archived memory", MemoryUnitStatus.ARCHIVED))
    store.create_memory_unit(make_unit("Other project memory", MemoryUnitStatus.CRYSTAL, "other"))

    hits = store.search_memory_units_for_query("membox", "Phase E query memory")

    assert [hit["id"] for hit in hits[:2]] == [crystal_id, active_id]
    assert hits[0]["score"] > hits[1]["score"]
    assert {"crystal", "active_unit", "crystal_candidate"}.issubset({hit["status"] for hit in hits})

    all_project_hits = store.search_memory_units_for_query(None, "Phase E query memory")
    assert {hit["project"] for hit in all_project_hits} == {"membox", "other"}


def test_memory_cli_dry_run_and_apply_are_idempotent(tmp_path: Path) -> None:
    """memory triage/extract obey dry-run/apply and source-id dedup."""
    db = str(tmp_path / "cli.db")
    store = KnowledgeStore(db)
    session = HistorySessionRecord(
        id="membox-capture:s1",
        external_id="s1",
        project="membox",
        title="test",
        source_kind=SourceKind.MEMBOX_CAPTURE,
        source_ref="fixture.jsonl",
    )
    message = HistoryMessageRecord(
        id="membox-capture:s1:msg:1",
        session_id=session.id,
        external_id="1",
        role="user",
        text="Remember this rule: always run migration 8 checks before Phase C storage work.",
        created_at="2026-06-11T00:00:00",
    )
    store.upsert_history_session(session)
    store.upsert_history_messages(session, [message])

    dry = runner.invoke(app, ["memory", "triage", "--db", db, "--project", "membox", "--dry-run"])
    assert dry.exit_code == 0
    assert "extract=True" in dry.output
    assert store._conn().execute("SELECT COUNT(*) FROM history_triage;").fetchone()[0] == 0
    # (b) dry-run must not acquire lifecycle_lease:<project> in the meta table.
    assert (
        store._conn()
        .execute(
            "SELECT COUNT(*) FROM meta WHERE key=?;",
            ("lifecycle_lease:membox",),
        )
        .fetchone()[0]
        == 0
    ), "dry-run must not write a lifecycle_lease row to the meta table"

    triage = runner.invoke(app, ["memory", "triage", "--db", db, "--project", "membox", "--apply"])
    assert triage.exit_code == 0
    assert store._conn().execute("SELECT COUNT(*) FROM history_triage;").fetchone()[0] == 1
    assert (
        store._conn().execute("SELECT gate_version FROM history_triage;").fetchone()[0]
        == GATE_VERSION
    )

    extract = runner.invoke(
        app, ["memory", "extract", "--db", db, "--project", "membox", "--apply"]
    )
    assert extract.exit_code == 0
    assert store._conn().execute("SELECT COUNT(*) FROM memory_units;").fetchone()[0] == 1

    # Re-triage under the same gate version and extract again: source identity
    # dedup prevents a second unit.
    runner.invoke(app, ["memory", "triage", "--db", db, "--project", "membox", "--apply"])
    extract_again = runner.invoke(
        app, ["memory", "extract", "--db", db, "--project", "membox", "--apply"]
    )
    assert extract_again.exit_code == 0
    assert store._conn().execute("SELECT COUNT(*) FROM memory_units;").fetchone()[0] == 1


def test_memory_cli_accepts_temporary_v3_gate_escape_hatch(tmp_path: Path) -> None:
    """Explicit --gate heuristic-v3 rows are still consumable for one release."""
    db = str(tmp_path / "legacy-gate.db")
    store = KnowledgeStore(db)
    session = HistorySessionRecord(
        id="membox-capture:s1",
        external_id="s1",
        project="membox",
        title="test",
        source_kind=SourceKind.MEMBOX_CAPTURE,
        source_ref="fixture.jsonl",
    )
    message = HistoryMessageRecord(
        id="membox-capture:s1:msg:1",
        session_id=session.id,
        external_id="1",
        role="user",
        text="Remember this rule: always run migration checks before storage work.",
        created_at="2026-06-11T00:00:00",
    )
    store.upsert_history_session(session)
    store.upsert_history_messages(session, [message])

    triage = runner.invoke(
        app,
        [
            "memory",
            "triage",
            "--db",
            db,
            "--project",
            "membox",
            "--gate",
            "heuristic-v3",
            "--apply",
        ],
    )
    extract = runner.invoke(
        app,
        [
            "memory",
            "extract",
            "--db",
            db,
            "--project",
            "membox",
            "--gate",
            "heuristic-v3",
            "--apply",
        ],
    )

    assert triage.exit_code == 0, triage.output
    assert extract.exit_code == 0, extract.output
    assert (
        store._conn().execute("SELECT gate_version FROM history_triage;").fetchone()[0]
        == "heuristic-v3"
    )
    assert store._conn().execute("SELECT COUNT(*) FROM memory_units;").fetchone()[0] == 1
