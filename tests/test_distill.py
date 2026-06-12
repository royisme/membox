"""Lifecycle Phase F workflow-distillation tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from membox.cli import app
from membox.core.distill import AssetRecord, FilesystemAssetInventory, build_distill_plan
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


def _history_source(session: str) -> MemoryUnitSource:
    """Return a history message source for one session."""
    return MemoryUnitSource(
        source_kind=MemorySourceKind.HISTORY_MESSAGE,
        source_ref=f"membox-capture:{session}:msg:m1",
        source_message_id=f"membox-capture:{session}:msg:m1",
        quote="Always verify latest_version before adding a migration.",
    )


def _unit(session: str, *, title: str | None = None) -> MemoryUnitRecord:
    """Return a procedure unit for distill tests."""
    return MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.PROCEDURE,
        status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
        title=title or "Verify migration head before editing migrations",
        content=(
            "Always verify latest_version before adding a migration after stale worktree "
            "migration-numbering failures."
        ),
        context="repeatable migration-head check workflow",
        importance_score=0.7,
        confidence_score=0.7,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow", "testing"],
        sources=[_history_source(session)],
    )


def _insert_session(store: KnowledgeStore, session: str) -> None:
    """Insert one history session plus its message source."""
    record = HistorySessionRecord(
        id=f"membox-capture:{session}",
        external_id=session,
        project="membox",
        source_kind=SourceKind.MEMBOX_CAPTURE,
        source_ref=f"{session}.jsonl",
    )
    store.upsert_history_session(record)
    store.upsert_history_messages(
        record,
        [
            HistoryMessageRecord(
                id=f"membox-capture:{session}:msg:m1",
                session_id=record.id,
                external_id="m1",
                role="user",
                text="Always verify latest_version before adding a migration.",
            )
        ],
    )


def test_distill_requires_two_independent_sessions(tmp_path: Path) -> None:
    """Single-session workflow evidence does not pass the candidate gate."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    unit_id = store.create_memory_unit(_unit("s1"))
    unit = store.get_memory_unit(unit_id)
    assert unit is not None

    plan = build_distill_plan(
        [unit],
        store.count_independent_sources_for_units([unit_id]),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert plan.scanned_unit_count == 1
    assert plan.candidates == []


def test_distill_groups_repeated_workflow_and_reports_script_form(tmp_path: Path) -> None:
    """Two matching workflow memories from distinct sessions become one candidate."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    _insert_session(store, "s2")
    first = store.create_memory_unit(_unit("s1"))
    second = store.create_memory_unit(
        _unit("s2", title="Verify latest_version before migration edits")
    )
    units = store.list_units_for_distill(project="membox")

    plan = build_distill_plan(
        units,
        store.count_independent_sources_for_units([first, second]),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.evidence_sessions == 2
    assert candidate.unit_count == 2
    assert candidate.recommended_form == "script"
    assert [member.unit_id for member in candidate.members] == [first, second]


def test_distill_reports_existing_matching_asset(tmp_path: Path) -> None:
    """Matching assets are reported as coverage instead of suppressing candidates."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    _insert_session(store, "s2")
    first = store.create_memory_unit(_unit("s1"))
    second = store.create_memory_unit(
        _unit("s2", title="Verify latest_version before migration edits")
    )
    units = store.list_units_for_distill(project="membox")

    plan = build_distill_plan(
        units,
        store.count_independent_sources_for_units([first, second]),
        assets=[AssetRecord("script", "verify migration head", "scripts/verify-migration-head.py")],
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert plan.candidates[0].covered_by == "scripts/verify-migration-head.py"


def test_filesystem_asset_inventory_reports_paths_from_root(tmp_path: Path) -> None:
    """Filesystem inventory reports names and root-relative paths only."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "verify-migration.md").write_text(
        "# command\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify-migration-head.py").write_text(
        "# script\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "migration-check").mkdir(parents=True)
    (tmp_path / "skills" / "migration-check" / "SKILL.md").write_text(
        "# skill\n",
        encoding="utf-8",
    )

    assets = FilesystemAssetInventory().list_assets(tmp_path)

    assert (
        AssetRecord("command", "verify-migration", ".claude/commands/verify-migration.md") in assets
    )
    assert (
        AssetRecord("script", "verify-migration-head", "scripts/verify-migration-head.py") in assets
    )
    assert AssetRecord("skill_file", "migration-check", "skills/migration-check/SKILL.md") in assets


def test_distill_cli_is_read_only_and_prints_empty_result(tmp_path: Path) -> None:
    """The CLI requires dry-run and does not take the lifecycle lease."""
    db = str(tmp_path / "memory.db")
    root = tmp_path / "project"
    root.mkdir()

    result = runner.invoke(
        app,
        ["distill", "--db", db, "--project", "membox", "--root", str(root), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "window: all" in result.output
    assert f"scanned_root: {root}" in result.output
    assert "no distill candidates found" in result.output
    assert "created nothing" in result.output
    store = KnowledgeStore(db)
    assert (
        store._conn()
        .execute("SELECT COUNT(*) FROM meta WHERE key='lifecycle_lease:membox'")
        .fetchone()[0]
        == 0
    )


def test_distill_cli_rejects_apply_and_missing_root(tmp_path: Path) -> None:
    """Phase F reserves apply and requires an explicit existing root."""
    apply_result = runner.invoke(app, ["distill", "--db", str(tmp_path / "memory.db"), "--apply"])
    assert apply_result.exit_code == 1
    assert "--apply is not implemented in Phase F" in apply_result.output

    root_result = runner.invoke(
        app,
        [
            "distill",
            "--db",
            str(tmp_path / "memory.db"),
            "--project",
            "membox",
            "--root",
            str(tmp_path / "missing"),
            "--dry-run",
        ],
    )
    assert root_result.exit_code == 1
    assert "--root does not exist" in root_result.output


# ---------------------------------------------------------------------------
# Bridged-group merging (review finding #1)
# ---------------------------------------------------------------------------


def test_distill_merges_bridged_groups(tmp_path: Path) -> None:
    """A unit that bridges two disjoint groups merges them into one."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    # Unit A: migration-check workflow
    _insert_session(store, "sa")
    a_id = store.create_memory_unit(_unit("sa", title="Verify migration head before editing"))
    # Unit B: migration-check + lint workflow (bridges A and C)
    _insert_session(store, "sb")
    b_id = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.PROCEDURE,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="Always verify migration head and run lint before commit",
            content=("Verify latest_version and run ruff before committing migration changes."),
            context="migration-and-lint workflow",
            importance_score=0.7,
            confidence_score=0.7,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["workflow"],
            sources=[_history_source("sb")],
        )
    )
    # Unit C: lint workflow (no direct overlap with A)
    _insert_session(store, "sc")
    c_id = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.PROCEDURE,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="Run ruff lint before commit",
            content="Always run ruff lint check before committing any changes.",
            context="lint workflow",
            importance_score=0.6,
            confidence_score=0.6,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["workflow"],
            sources=[_history_source("sc")],
        )
    )

    units = store.list_units_for_distill(project="membox")
    unit_ids = [unit.id for unit in units if unit.id is not None]

    plan = build_distill_plan(
        units,
        store.count_independent_sources_for_units(unit_ids),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.unit_count == 3
    assert {a_id, b_id, c_id} == {member.unit_id for member in candidate.members}


# ---------------------------------------------------------------------------
# MANUAL-source bypass (review finding #3)
# ---------------------------------------------------------------------------


def test_distill_produces_candidate_with_explicit_user_approval(tmp_path: Path) -> None:
    """A single-session unit with explicit user approval (MANUAL source) qualifies."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    manual_unit = MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.PROCEDURE,
        status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
        title="Verify migration head migration head check run ruff before editing",
        content="Always verify latest_version before adding a migration.",
        context="repeatable migration-head check workflow",
        importance_score=0.7,
        confidence_score=0.7,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow", "testing"],
        sources=[
            MemoryUnitSource(
                source_kind=MemorySourceKind.MANUAL,
                source_ref="user-confirmed",
                quote="I confirm this workflow should be packaged.",
            )
        ],
    )
    unit_id = store.create_memory_unit(manual_unit)
    unit = store.get_memory_unit(unit_id)
    assert unit is not None

    plan = build_distill_plan(
        [unit],
        store.count_independent_sources_for_units([unit_id]),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert plan.scanned_unit_count == 1
    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert "explicit_user_approval=True" in candidate.explain


# ---------------------------------------------------------------------------
# recommend_form variants (review finding #3)
# ---------------------------------------------------------------------------


def test_recommend_form_returns_command_for_cli_tokens(tmp_path: Path) -> None:
    """Procedure units with CLI-related tokens recommend 'command' form."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    _insert_session(store, "s2")
    first = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.PROCEDURE,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="Use the CLI command to deploy",
            content="Always run the deploy command via the CLI before merging.",
            context="deploy workflow",
            importance_score=0.7,
            confidence_score=0.7,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["cli", "tooling"],
            sources=[_history_source("s1")],
        )
    )
    second = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.PROCEDURE,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="CLI deploy command workflow",
            content="Always run the deploy command via the CLI before merging.",
            context="deploy workflow",
            importance_score=0.7,
            confidence_score=0.7,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["cli", "tooling"],
            sources=[_history_source("s2")],
        )
    )
    unit_ids = [first, second]

    plan = build_distill_plan(
        store.list_units_for_distill(project="membox"),
        store.count_independent_sources_for_units(unit_ids),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].recommended_form == "command"


def test_recommend_form_returns_convention_doc_for_pure_learning(tmp_path: Path) -> None:
    """Pure LEARNING units recommend 'convention_doc' form."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    _insert_session(store, "s2")
    first = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.LEARNING,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="Use snake_case for all Python identifiers",
            content="Always use snake_case naming convention for Python code.",
            context="naming convention",
            importance_score=0.7,
            confidence_score=0.7,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["conventions"],
            sources=[_history_source("s1")],
        )
    )
    second = store.create_memory_unit(
        MemoryUnitRecord(
            project="membox",
            unit_type=MemoryUnitType.LEARNING,
            status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            title="snake_case for Python identifiers convention",
            content="Always use snake_case naming convention for Python code.",
            context="naming convention",
            importance_score=0.7,
            confidence_score=0.7,
            temporal_type=MemoryTemporalType.UNKNOWN,
            labels=["conventions"],
            sources=[_history_source("s2")],
        )
    )
    unit_ids = [first, second]

    plan = build_distill_plan(
        store.list_units_for_distill(project="membox"),
        store.count_independent_sources_for_units(unit_ids),
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    assert len(plan.candidates) >= 1
    assert plan.candidates[0].recommended_form == "convention_doc"


def test_recommend_form_returns_skill_file_for_mixed_types(tmp_path: Path) -> None:
    """Mixed PROCEDURE+LEARNING units recommend 'skill_file' form."""
    from membox.core.distill import recommend_form

    proc = MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.PROCEDURE,
        status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
        title="Run the agent workflow",
        content="Always run agent workflow before deploying changes.",
        context="workflow",
        importance_score=0.7,
        confidence_score=0.7,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow"],
        sources=[_history_source("s1")],
    )
    learn = MemoryUnitRecord(
        project="membox",
        unit_type=MemoryUnitType.LEARNING,
        status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
        title="Workflow reuse pattern",
        content="Agent workflow is a reusable pattern for deploying changes.",
        context="workflow",
        importance_score=0.7,
        confidence_score=0.7,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow", "architecture"],
        sources=[_history_source("s2")],
    )

    # PROCEDURE alone with SKILL_TERMS → skill_file (step 4 fires before fallback)
    assert recommend_form([proc]) == "skill_file"
    # LEARNING alone → convention_doc (step 3)
    assert recommend_form([learn]) == "convention_doc"
    # Mixed types → skill_file (step 4: len(unit_types) > 1)
    assert recommend_form([proc, learn]) == "skill_file"


# ---------------------------------------------------------------------------
# covered_by form-mismatch (review finding #3)
# ---------------------------------------------------------------------------


def test_covered_by_does_not_match_wrong_form(tmp_path: Path) -> None:
    """Assets with a different form than recommended do not trigger coverage."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    _insert_session(store, "s1")
    _insert_session(store, "s2")
    store.create_memory_unit(_unit("s1"))
    store.create_memory_unit(_unit("s2", title="Verify latest_version before migration edits"))
    units = store.list_units_for_distill(project="membox")
    unit_ids = [unit.id for unit in units if unit.id is not None]

    plan = build_distill_plan(
        units,
        store.count_independent_sources_for_units(unit_ids),
        assets=[
            AssetRecord("command", "verify migration head", ".claude/commands/verify-migration.md")
        ],
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    # recommended_form is "script" (migration+verify tokens); the asset is "command"
    assert len(plan.candidates) >= 1
    assert plan.candidates[0].recommended_form == "script"
    assert plan.candidates[0].covered_by is None


# ---------------------------------------------------------------------------
# --since window (review finding #3)
# ---------------------------------------------------------------------------


def test_distill_cli_with_since_window(tmp_path: Path) -> None:
    """The --since flag prints the effective window label."""
    db = str(tmp_path / "memory.db")
    root = tmp_path / "project"
    root.mkdir()

    result = runner.invoke(
        app,
        [
            "distill",
            "--db",
            db,
            "--project",
            "membox",
            "--root",
            str(root),
            "--since",
            "30d",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "window: since 30d" in result.output
    assert "no distill candidates found" in result.output
