"""Lifecycle Phase D memory-consolidation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from membox.cli import app
from membox.core.consolidate import build_consolidation_plan, crystal_policy
from membox.core.store import KnowledgeStore
from membox.model.schema import (
    HistoryMessageRecord,
    HistorySessionRecord,
    MemorySourceKind,
    MemoryTemporalType,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    MemoryUnitType,
    SourceKind,
)

runner = CliRunner()


def _source(
    ref: str = "manual:1", kind: MemorySourceKind = MemorySourceKind.MANUAL
) -> MemoryUnitSource:
    """Return one valid memory-unit source."""
    return MemoryUnitSource(source_kind=kind, source_ref=ref, source_message_id=ref, quote=ref)


def _unit(
    *,
    title: str,
    content: str,
    unit_type: MemoryUnitType = MemoryUnitType.DECISION,
    status: MemoryUnitStatus = MemoryUnitStatus.ACTIVE_UNIT,
    importance: float = 0.8,
    confidence: float = 0.75,
    labels: list[str] | None = None,
    sources: list[MemoryUnitSource] | None = None,
    valid_to: str | None = None,
) -> MemoryUnitRecord:
    """Return a valid memory unit for Phase D tests."""
    return MemoryUnitRecord(
        project="membox",
        unit_type=unit_type,
        status=status,
        title=title,
        content=content,
        importance_score=importance,
        confidence_score=confidence,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow"] if labels is None else labels,
        sources=[_source(f"manual:{title}", MemorySourceKind.MANUAL)]
        if sources is None
        else sources,
        valid_to=valid_to,
    )


def test_crystal_policy_requires_source_and_threshold() -> None:
    """Automatic crystals require accepted source and score thresholds."""
    no_source = _unit(title="No source", content="No source")
    no_source.sources = []
    assert crystal_policy(no_source, 0).eligible is False

    one_auto_source = _unit(
        title="Auto decision",
        content="Decision is not yet strong enough.",
        sources=[_source("history:1", MemorySourceKind.HISTORY_MESSAGE)],
    )
    assert crystal_policy(one_auto_source, 1).eligible is False

    three_sources = crystal_policy(one_auto_source, 3)
    assert three_sources.eligible is True
    assert three_sources.reason == "independent_source_count>=3"

    high_confidence = _unit(
        title="High confidence decision",
        content="Decision reached confidence through score evolution.",
        confidence=0.90,
        sources=[_source("history:high-confidence", MemorySourceKind.HISTORY_MESSAGE)],
    )
    assert crystal_policy(high_confidence, 1).reason == "high_confidence_decision"


def test_independent_source_count_uses_distinct_sessions(tmp_path: Path) -> None:
    """Repeated evidence inside one history session counts as one source."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    session = HistorySessionRecord(
        id="membox-capture:s1",
        external_id="s1",
        project="membox",
        source_kind=SourceKind.MEMBOX_CAPTURE,
        source_ref="fixture.jsonl",
    )
    store.upsert_history_session(session)
    store.upsert_history_messages(
        session,
        [
            HistoryMessageRecord(
                id="membox-capture:s1:msg:1",
                session_id=session.id,
                external_id="1",
                role="user",
                text="first",
            ),
            HistoryMessageRecord(
                id="membox-capture:s1:msg:2",
                session_id=session.id,
                external_id="2",
                role="user",
                text="second",
            ),
        ],
    )
    unit_id = store.create_memory_unit(
        _unit(
            title="Repeated source",
            content="Two messages in one session.",
            sources=[
                _source("membox-capture:s1:msg:1", MemorySourceKind.HISTORY_MESSAGE),
                _source("membox-capture:s1:msg:2", MemorySourceKind.HISTORY_MESSAGE),
            ],
        )
    )

    assert store.count_independent_sources(unit_id) == 1

    assert store.attach_memory_unit_source(unit_id, _source("doc-a", MemorySourceKind.DOCUMENT))
    assert store.count_independent_sources(unit_id) == 2
    updated = store.get_memory_unit(unit_id)
    assert updated is not None
    assert updated.confidence_score == pytest.approx(0.80)
    assert store.count_independent_sources_for_units([unit_id]) == {unit_id: 2}


def test_consolidate_dry_run_does_not_write_or_take_lease(tmp_path: Path) -> None:
    """Dry-run previews actions without status changes or lifecycle lease writes."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    unit_id = store.create_memory_unit(
        _unit(
            title="Confirmed decision",
            content="Owner confirmed this decision.",
            confidence=0.90,
            sources=[_source("history:1", MemorySourceKind.HISTORY_MESSAGE)],
        )
    )

    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert f"would promote {unit_id} -> crystal" in result.output
    unit = store.get_memory_unit(unit_id)
    assert unit is not None
    assert unit.status == MemoryUnitStatus.ACTIVE_UNIT
    assert (
        store._conn()
        .execute("SELECT COUNT(*) FROM meta WHERE key='lifecycle_lease:membox'")
        .fetchone()[0]
        == 0
    )


def test_consolidate_apply_promotes_candidates_and_logs(tmp_path: Path) -> None:
    """Apply performs audited Phase D transitions."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    crystal_id = store.create_memory_unit(
        _unit(
            title="Confirmed decision",
            content="Owner confirmed this decision.",
            confidence=0.90,
            sources=[_source("history:1", MemorySourceKind.HISTORY_MESSAGE)],
        )
    )
    candidate_id = store.create_memory_unit(
        _unit(
            title="Verify migration head",
            content="Failure happened. Always verify latest_version before migrations.",
            unit_type=MemoryUnitType.PROCEDURE,
            importance=0.65,
            confidence=0.65,
            labels=["testing"],
            sources=[_source("history:2", MemorySourceKind.HISTORY_MESSAGE)],
        )
    )

    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--apply"],
    )

    assert result.exit_code == 0, result.output
    crystal = store.get_memory_unit(crystal_id)
    candidate = store.get_memory_unit(candidate_id)
    assert crystal is not None
    assert candidate is not None
    assert crystal.status == MemoryUnitStatus.CRYSTAL
    assert candidate.status == MemoryUnitStatus.CRYSTAL_CANDIDATE
    rows = (
        store._conn()
        .execute(
            """
            SELECT to_status, command FROM memory_unit_status_log
            WHERE unit_id IN (?, ?) AND command='memory consolidate'
            ORDER BY id
            """,
            (crystal_id, candidate_id),
        )
        .fetchall()
    )
    assert rows == [("crystal", "memory consolidate"), ("crystal_candidate", "memory consolidate")]


def test_consolidation_surfaces_conflict_without_overwriting(tmp_path: Path) -> None:
    """Conflicting units are reported and left active."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    first = store.create_memory_unit(
        _unit(
            title="Feature branch workflow",
            content="Always branch feature work from develop and open PRs back to develop.",
        )
    )
    second = store.create_memory_unit(
        _unit(
            title="Release branch workflow",
            content="For release-only work, branch from main instead of develop. This conflicts.",
        )
    )
    units = store.list_units_for_consolidation(project="membox")
    plan = build_consolidation_plan(
        units,
        {
            unit.id: store.count_independent_sources(unit.id)
            for unit in units
            if unit.id is not None
        },
    )

    assert [(c.left_id, c.right_id) for c in plan.conflicts] == [(first, second)]
    assert plan.supersessions == []
    first_unit = store.get_memory_unit(first)
    second_unit = store.get_memory_unit(second)
    assert first_unit is not None
    assert second_unit is not None
    assert first_unit.status == MemoryUnitStatus.ACTIVE_UNIT
    assert second_unit.status == MemoryUnitStatus.ACTIVE_UNIT


def test_consolidation_supersedes_newer_corrections(tmp_path: Path) -> None:
    """A newer corrective unit supersedes the stale older unit."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    old_id = store.create_memory_unit(
        _unit(
            title="Old fact",
            content="Fact snapshot: membox query currently uses graph-only retrieval.",
            unit_type=MemoryUnitType.FACT,
            labels=["retrieval"],
        )
    )
    new_id = store.create_memory_unit(
        _unit(
            title="Updated fact",
            content="Updated fact: membox query now uses graph plus FTS fusion; old statement stale.",
            unit_type=MemoryUnitType.FACT,
            labels=["retrieval"],
        )
    )

    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--apply"],
    )

    assert result.exit_code == 0, result.output
    old = store.get_memory_unit(old_id)
    assert old is not None
    assert old.status == MemoryUnitStatus.SUPERSEDED
    assert old.superseded_by == new_id


def test_consolidation_decay_archives_expired_non_plan_units(tmp_path: Path) -> None:
    """Expired factual units archive, while expired plan/context units are only surfaced."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    fact_id = store.create_memory_unit(
        _unit(
            title="Expired fact",
            content="A factual unit with expired validity.",
            unit_type=MemoryUnitType.FACT,
            valid_to="2000-01-01T00:00:00Z",
        )
    )
    plan_id = store.create_memory_unit(
        _unit(
            title="Expired plan",
            content="A plan whose review horizon passed.",
            unit_type=MemoryUnitType.PLAN,
            valid_to="2000-01-01T00:00:00Z",
        )
    )

    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--apply"],
    )

    assert result.exit_code == 0, result.output
    assert "decay review" in result.output
    fact = store.get_memory_unit(fact_id)
    plan = store.get_memory_unit(plan_id)
    assert fact is not None
    assert plan is not None
    assert fact.status == MemoryUnitStatus.ARCHIVED
    assert plan.status == MemoryUnitStatus.ACTIVE_UNIT
