"""Tests for `membox memory export --as memory-md` (B1)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner, Result

from membox.cli import app
from membox.core.export import (
    SECTION_ARCHITECTURE,
    SECTION_GOTCHAS,
    SECTION_KNOWLEDGE,
    SECTION_OTHER,
    SECTION_RULES,
    categorize_for_export,
)
from membox.core.store import KnowledgeStore
from membox.model.schema import (
    MemorySourceKind,
    MemoryTemporalType,
    MemoryUnitRecord,
    MemoryUnitStatus,
    MemoryUnitType,
)
from tests.test_consolidation import _source, _unit

runner = CliRunner()


def test_categorize_routes_decision_to_architecture() -> None:
    """A DECISION unit lands under Architecture decisions."""
    unit = _unit(
        title="Migrate to SQLite",
        content="Move all storage to local SQLite database.",
        unit_type=MemoryUnitType.DECISION,
        sources=[_source("manual:decision-1")],
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_ARCHITECTURE: [unit]}


def test_categorize_routes_procedure_to_rules() -> None:
    """A PROCEDURE unit lands under Rules / Conventions."""
    unit = _unit(
        title="Always run tests",
        content="Run the test suite before merging.",
        unit_type=MemoryUnitType.PROCEDURE,
        sources=[_source("manual:rule-1")],
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_RULES: [unit]}


def test_categorize_routes_fact_to_discovered() -> None:
    """A FACT unit lands under Discovered durable knowledge."""
    unit = _unit(
        title="Membox uses SQLite",
        content="Membox uses local SQLite for storage as the default.",
        unit_type=MemoryUnitType.FACT,
        sources=[_source("manual:fact-1")],
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_KNOWLEDGE: [unit]}


def test_categorize_security_label_routes_to_gotchas() -> None:
    """A unit carrying the `security` label routes to Gotchas."""
    unit = _unit(
        title="Tokens live in session env",
        content="Tokens live in session env, never commit them.",
        unit_type=MemoryUnitType.FACT,
        labels=["security"],
        sources=[_source("manual:security-1")],
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_GOTCHAS: [unit]}


def test_categorize_unsupported_claim_routes_to_gotchas() -> None:
    """A unit whose content shares <2 tokens with its source quote is a gotcha."""
    sources = [
        _source(
            "history:gotcha-1",
            MemorySourceKind.HISTORY_MESSAGE,
            quote="alpha bravo charlie delta echo",
        )
    ]
    unit = MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.FACT,
        status=MemoryUnitStatus.ACTIVE_UNIT,
        title="Unrelated title",
        content="completely different claim words here",
        importance_score=0.8,
        confidence_score=0.7,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow"],
        sources=sources,
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_GOTCHAS: [unit]}


def test_categorize_precedence_gotchas_wins_over_architecture() -> None:
    """A DECISION with a security label routes to Gotchas (not Architecture)."""
    unit = _unit(
        title="Auth via short-lived tokens",
        content="Auth uses short-lived tokens only.",
        unit_type=MemoryUnitType.DECISION,
        labels=["security"],
        sources=[_source("manual:auth-1")],
    )
    grouped = categorize_for_export([unit])
    assert grouped == {SECTION_GOTCHAS: [unit]}


def test_categorize_uses_other_for_unmatched() -> None:
    """CONTEXT/PLAN/EVENT units with no extra signal fall to Other."""
    context_unit = _unit(
        title="Phase background",
        content="Context about the current phase.",
        unit_type=MemoryUnitType.CONTEXT,
        sources=[_source("manual:context-1")],
    )
    plan_unit = _unit(
        title="Migrate in Q3",
        content="Migrate retrieval in Q3.",
        unit_type=MemoryUnitType.PLAN,
        sources=[_source("manual:plan-1")],
    )
    grouped = categorize_for_export([context_unit, plan_unit])
    assert grouped == {SECTION_OTHER: [context_unit, plan_unit]}


def test_export_command_emits_markdown_with_provenance(tmp_path: Path) -> None:
    """`memory export --as memory-md` renders sections + provenance suffixes."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    store.create_memory_unit(
        _unit(
            title="Use local SQLite",
            content="Membox stores data in local SQLite database.",
            unit_type=MemoryUnitType.DECISION,
            sources=[_source("manual:decision-md", quote="sqlite decision source quote")],
        )
    )
    store.create_memory_unit(
        _unit(
            title="Run tests before merge",
            content="Always run pytest before merging changes.",
            unit_type=MemoryUnitType.PROCEDURE,
            sources=[_source("manual:procedure-md")],
        )
    )

    result: Result = runner.invoke(
        app,
        [
            "memory",
            "export",
            "--as",
            "memory-md",
            "--db",
            db,
            "--project",
            "membox",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# Memory — membox" in result.output
    assert "## Architecture decisions" in result.output
    assert "## Rules / Conventions" in result.output
    assert "manual:decision-md" in result.output
    assert "manual:procedure-md" in result.output
    assert "Use local SQLite" in result.output


def test_export_command_crystals_only_filters(tmp_path: Path) -> None:
    """`--crystals-only` restricts the export to crystal-status units."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    store.create_memory_unit(
        _unit(
            title="Active decision",
            content="An active decision that should not appear.",
            unit_type=MemoryUnitType.DECISION,
            sources=[_source("manual:active-md")],
        )
    )
    store.create_memory_unit(
        _unit(
            title="Crystal rule",
            content="An established rule kept as a crystal.",
            unit_type=MemoryUnitType.PROCEDURE,
            status=MemoryUnitStatus.CRYSTAL,
            sources=[_source("manual:crystal-md")],
        )
    )

    result: Result = runner.invoke(
        app,
        [
            "memory",
            "export",
            "--as",
            "memory-md",
            "--crystals-only",
            "--db",
            db,
            "--project",
            "membox",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "manual:crystal-md" in result.output
    assert "manual:active-md" not in result.output
    assert "An active decision that should not appear." not in result.output


def test_export_command_bogus_format_exits_1(tmp_path: Path) -> None:
    """Unsupported --as values exit non-zero with a clear error message."""
    db = str(tmp_path / "memory.db")
    result: Result = runner.invoke(
        app,
        ["memory", "export", "--as", "bogus", "--db", db, "--project", "membox"],
    )
    assert result.exit_code == 1
    combined = (result.output or "") + (result.stderr or "")
    assert "memory-md" in combined
