"""Lifecycle Phase D memory-consolidation tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from membox.cli import app
from membox.core.consolidate import (
    CRYSTAL_MAX_CONTENT_LENGTH,
    ConsolidationConflict,
    ConsolidationPlan,
    ConsolidationTransition,
    build_consolidation_plan,
    crystal_policy,
    validate_units,
)
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
from membox.services.extraction import ComparatorScore

runner = CliRunner()


class LowScoreComparator:
    """Fake comparator that drops the configured unit IDs."""

    def __init__(self, low_score_ids: set[int]) -> None:
        self.low_score_ids = low_score_ids

    def rescore_candidates(
        self,
        candidates: list[MemoryUnitRecord],
        surrounding_units: list[MemoryUnitRecord],
    ) -> list[ComparatorScore]:
        """Return deterministic scores for tests without calling an LLM."""
        _ = surrounding_units
        return [
            ComparatorScore(
                unit_id=unit.id or 0, score=0.10 if unit.id in self.low_score_ids else 1
            )
            for unit in candidates
        ]


_UNSET: object = object()
"""Sentinel meaning 'argument not provided' — lets tests pass an explicit None.

Typed as ``object`` so the ``_unit()`` helper can accept an explicit
``None`` (the "missing rationale" case) without mypy narrowing it back to
``str | None`` at the call site.
"""


def _source(
    ref: str = "manual:1",
    kind: MemorySourceKind = MemorySourceKind.MANUAL,
    *,
    quote: str | None = None,
) -> MemoryUnitSource:
    """Return one valid memory-unit source."""
    text = ref if quote is None else quote
    return MemoryUnitSource(source_kind=kind, source_ref=ref, source_message_id=ref, quote=text)


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
    context: str = "",
    why: str | None | object = _UNSET,
    how_to_apply: str | None | object = _UNSET,
    next_step: str | None | object = _UNSET,
) -> MemoryUnitRecord:
    """Return a valid memory unit for Phase D tests.

    Defaults set ``why="test rationale"`` (and ``how_to_apply``/``next_step``
    for PROCEDURE/PLAN) so most existing tests pass the M4 Part A2 rationale
    gate.  Tests that exercise the gate explicitly pass ``None``.
    """
    if why is _UNSET:
        why = "test rationale"
    if how_to_apply is _UNSET:
        how_to_apply = "test procedure recipe" if unit_type == MemoryUnitType.PROCEDURE else None
    if next_step is _UNSET:
        next_step = (
            "test next step"
            if unit_type in (MemoryUnitType.PROCEDURE, MemoryUnitType.PLAN)
            else None
        )
    return MemoryUnitRecord(
        project="membox",
        unit_type=unit_type,
        status=status,
        title=title,
        content=content,
        context=context,
        importance_score=importance,
        confidence_score=confidence,
        temporal_type=MemoryTemporalType.UNKNOWN,
        labels=["workflow"] if labels is None else labels,
        sources=[_source(f"manual:{title}", MemorySourceKind.MANUAL)]
        if sources is None
        else sources,
        valid_to=valid_to,
        why=cast("str | None", why),
        how_to_apply=cast("str | None", how_to_apply),
        next_step=cast("str | None", next_step),
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
            content=(
                "Owner confirmed this decision during the planning review meeting "
                "with explicit user confirmation recorded in the project log."
            ),
            confidence=0.90,
            sources=[
                _source(
                    "history:1",
                    MemorySourceKind.HISTORY_MESSAGE,
                    quote="owner explicitly confirmed this decision during review meeting",
                )
            ],
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


def test_optional_llm_comparator_filters_low_scoring_transitions(tmp_path: Path) -> None:
    """Injected comparator can drop low-scoring transitions; default path is unchanged."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    unit_id = store.create_memory_unit(
        _unit(
            title="Confirmed decision",
            content=(
                "Owner confirmed this decision during the planning review meeting "
                "with explicit user confirmation recorded in the project log."
            ),
            confidence=0.90,
            sources=[
                _source(
                    "history:1",
                    MemorySourceKind.HISTORY_MESSAGE,
                    quote="owner explicitly confirmed this decision during review meeting",
                )
            ],
        )
    )
    units = store.list_units_for_consolidation(project="membox")
    counts = store.count_independent_sources_for_units([unit_id])

    default_plan = build_consolidation_plan(units, counts)
    filtered_plan = build_consolidation_plan(
        units,
        counts,
        comparator=LowScoreComparator({unit_id}),
    )

    assert [transition.unit_id for transition in default_plan.promotions] == [unit_id]
    assert filtered_plan.promotions == []


def test_consolidation_plan_exposes_order_and_review_pairs() -> None:
    """Plan helpers keep apply ordering and review aggregation in the core module."""
    supersede = ConsolidationTransition(
        1, "supersede", MemoryUnitStatus.SUPERSEDED, "newer unit", superseded_by=2
    )
    archive = ConsolidationTransition(3, "archive", MemoryUnitStatus.ARCHIVED, "expired")
    promote = ConsolidationTransition(4, "promote", MemoryUnitStatus.CRYSTAL, "confirmed")
    candidate = ConsolidationTransition(
        5, "candidate", MemoryUnitStatus.CRYSTAL_CANDIDATE, "review"
    )
    demote = ConsolidationTransition(6, "demote", MemoryUnitStatus.ACTIVE_UNIT, "rejected")
    conflict = ConsolidationConflict(7, 8, "left", "right", "hard conflict", ["a"])
    fts_pair = ConsolidationConflict(9, 10, "fts left", "fts right", "fts pair", ["b"])
    plan = ConsolidationPlan(
        supersessions=[supersede],
        decay_archives=[archive],
        promotions=[promote],
        candidates=[candidate],
        demotions=[demote],
        conflicts=[conflict],
        fts_pairs=[fts_pair],
    )

    assert plan.ordered_transitions() == [supersede, archive, promote, candidate, demote]
    assert plan.transition_groups() == (
        ("supersede", [supersede]),
        ("archive", [archive]),
        ("promote", [promote]),
        ("candidate", [candidate]),
        ("demote", [demote]),
    )
    assert plan.review_pairs() == [conflict, fts_pair]


def test_consolidate_apply_promotes_candidates_and_logs(tmp_path: Path) -> None:
    """Apply performs audited Phase D transitions."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    crystal_id = store.create_memory_unit(
        _unit(
            title="Confirmed decision",
            content=(
                "Owner confirmed this decision during the planning review meeting "
                "with explicit user confirmation recorded in the project log."
            ),
            confidence=0.90,
            sources=[
                _source(
                    "history:1",
                    MemorySourceKind.HISTORY_MESSAGE,
                    quote="owner explicitly confirmed this decision during review meeting",
                )
            ],
        )
    )
    candidate_id = store.create_memory_unit(
        _unit(
            title="Verify migration head",
            content=(
                "Failure happened during the last migration. Always verify the "
                "latest_version before applying any database migration in CI."
            ),
            unit_type=MemoryUnitType.PROCEDURE,
            importance=0.65,
            confidence=0.65,
            labels=["testing"],
            sources=[
                _source(
                    "history:2",
                    MemorySourceKind.HISTORY_MESSAGE,
                    quote="failure happened during last migration verify latest version",
                )
            ],
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


def test_atomic_transition_batch_rolls_back_partial_failure(tmp_path: Path) -> None:
    """A mid-batch transition failure leaves no earlier transitions visible."""
    store = KnowledgeStore(str(tmp_path / "memory.db"))
    unit_ids = [
        store.create_memory_unit(
            _unit(
                title=f"Batch unit {index}",
                content=f"Batch unit {index} content for atomic apply testing.",
                unit_type=MemoryUnitType.PROCEDURE,
            )
        )
        for index in range(10)
    ]
    transitions = [
        ConsolidationTransition(
            unit_id=unit_id,
            title=f"Batch unit {index}",
            to_status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
            reason="test batch transition",
        )
        for index, unit_id in enumerate(unit_ids)
    ]
    failing_unit = unit_ids[6]
    transitions[6] = ConsolidationTransition(
        unit_id=failing_unit,
        title="Batch unit 6",
        to_status=MemoryUnitStatus.SUPERSEDED,
        reason="forced failure on seventh unit",
        superseded_by=failing_unit,
    )

    with pytest.raises(ValueError, match=f"unit cannot supersede itself: {failing_unit}"):
        store.transition_memory_units_atomically(transitions, command="memory consolidate")

    statuses_after_failure = []
    for unit_id in unit_ids:
        unit = store.get_memory_unit(unit_id)
        assert unit is not None
        statuses_after_failure.append(unit.status)
    assert statuses_after_failure == [MemoryUnitStatus.ACTIVE_UNIT] * 10
    log_count = (
        store._conn()
        .execute(
            """
            SELECT COUNT(*) FROM memory_unit_status_log
            WHERE command='memory consolidate'
            """
        )
        .fetchone()[0]
    )
    assert log_count == 0

    applied = store.transition_memory_units_atomically(
        [
            ConsolidationTransition(
                unit_id=unit_id,
                title=f"Batch unit {index}",
                to_status=MemoryUnitStatus.CRYSTAL_CANDIDATE,
                reason="retry after rollback",
            )
            for index, unit_id in enumerate(unit_ids)
        ],
        command="memory consolidate",
    )

    assert applied == 10
    statuses_after_retry = []
    for unit_id in unit_ids:
        unit = store.get_memory_unit(unit_id)
        assert unit is not None
        statuses_after_retry.append(unit.status)
    assert statuses_after_retry == [MemoryUnitStatus.CRYSTAL_CANDIDATE] * 10


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


def test_consolidation_surfaces_fts_pair_for_paraphrases(tmp_path: Path) -> None:
    """Paraphrased units are surfaced as review-only FTS pair candidates."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    first = store.create_memory_unit(
        _unit(
            title="SQLite storage default",
            content="Membox uses local SQLite storage as the default database for agents.",
            unit_type=MemoryUnitType.FACT,
            labels=["storage"],
            sources=[_source("manual:sqlite-a")],
        )
    )
    second = store.create_memory_unit(
        _unit(
            title="Local SQLite agent database",
            content="For agent memory, the default storage backend is a local SQLite database.",
            unit_type=MemoryUnitType.FACT,
            labels=["storage"],
            sources=[_source("manual:sqlite-b")],
        )
    )
    fts_pairs = store.fts_conflict_pairs_for_units(
        store.list_units_for_consolidation(project="membox")
    )
    plan = build_consolidation_plan(
        store.list_units_for_consolidation(project="membox"),
        {
            unit.id: store.count_independent_sources(unit.id)
            for unit in store.list_units_for_consolidation(project="membox")
            if unit.id is not None
        },
        fts_pair_ids=fts_pairs,
    )

    dry_run = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--dry-run"],
    )
    apply = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--apply"],
    )

    assert (first, second) in fts_pairs
    assert [(pair.left_id, pair.right_id) for pair in plan.fts_pairs] == [(first, second)]
    assert plan.conflicts == []
    assert {transition.unit_id for transition in plan.promotions} == {first, second}
    assert dry_run.exit_code == 0, dry_run.output
    assert f"conflict review {first}<->{second}" in dry_run.output
    assert "fts_pair token-overlap candidate" in dry_run.output
    assert apply.exit_code == 0, apply.output
    first_unit = store.get_memory_unit(first)
    second_unit = store.get_memory_unit(second)
    assert first_unit is not None
    assert second_unit is not None
    assert first_unit.status == MemoryUnitStatus.CRYSTAL
    assert second_unit.status == MemoryUnitStatus.CRYSTAL


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
            content=(
                "A factual unit documenting a snapshot whose validity window "
                "closed before the current consolidation run began processing it."
            ),
            unit_type=MemoryUnitType.FACT,
            valid_to="2000-01-01T00:00:00Z",
        )
    )
    plan_id = store.create_memory_unit(
        _unit(
            title="Expired plan",
            content=(
                "A plan whose review horizon passed; relevant context for the "
                "next planning iteration should be captured separately."
            ),
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


def test_crystal_budget_blocks_oversized_content() -> None:
    """Units whose content exceeds the crystal cap are rejected from promotion."""
    oversized_content = "x" * (CRYSTAL_MAX_CONTENT_LENGTH + 100)
    sources = [
        _source("history:a", MemorySourceKind.HISTORY_MESSAGE, quote=oversized_content),
        _source("history:b", MemorySourceKind.HISTORY_MESSAGE, quote=oversized_content),
        _source("history:c", MemorySourceKind.HISTORY_MESSAGE, quote=oversized_content),
    ]
    unit = _unit(
        title="Oversized content",
        content=oversized_content,
        sources=sources,
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    reasons = [issue.reason for issue in issues]
    assert any("content exceeds crystal budget" in reason for reason in reasons)

    counts = {1: 3}
    plan = build_consolidation_plan([unit], counts)
    assert plan.promotions == []


def test_crystal_budget_negative_clean_unit_under_cap_passes() -> None:
    """A compact unit with three sources clears the crystal budget gate."""
    sources = [
        _source("history:a", MemorySourceKind.HISTORY_MESSAGE),
        _source("history:b", MemorySourceKind.HISTORY_MESSAGE),
        _source("history:c", MemorySourceKind.HISTORY_MESSAGE),
    ]
    unit = _unit(
        title="Compact decision",
        content="Owner confirmed this compact decision with three independent sources.",
        sources=sources,
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert not any("content exceeds crystal budget" in issue.reason for issue in issues)


def test_provenance_strength_blocks_thin_sources() -> None:
    """A unit with a single source and no quote fails the provenance gate."""
    unit = _unit(
        title="Thin source",
        content="A claim supported by only one source and no quote.",
        sources=[_source("history:only", MemorySourceKind.HISTORY_MESSAGE, quote="")],
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    reasons = [issue.reason for issue in issues]
    assert "insufficient provenance for crystal (needs a quote or ≥2 sources)" in reasons


def test_provenance_strength_negative_passes_with_quote() -> None:
    """A single source with a non-empty quote satisfies the provenance gate."""
    unit = _unit(
        title="Quoted source",
        content="A claim with a source that carries an actual quote.",
        sources=[
            _source(
                "history:quoted",
                MemorySourceKind.HISTORY_MESSAGE,
                quote="some meaningful quote attached to this source",
            )
        ],
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert not any("insufficient provenance for crystal" in issue.reason for issue in issues)


def test_provenance_strength_negative_passes_with_two_refs() -> None:
    """Two distinct source_refs satisfy the provenance gate even without a quote."""
    unit = _unit(
        title="Two refs",
        content="A claim backed by two distinct references without quoted text.",
        sources=[
            _source("history:ref-a", MemorySourceKind.HISTORY_MESSAGE, quote=""),
            _source("history:ref-b", MemorySourceKind.HISTORY_MESSAGE, quote=""),
        ],
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert not any("insufficient provenance for crystal" in issue.reason for issue in issues)


def test_vague_content_flags_short_content() -> None:
    """A unit whose content is too thin is flagged as vague."""
    unit = _unit(
        title="Friendly acknowledgement",
        content="ok thanks",
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert "vague content (insufficient signal)" in [issue.reason for issue in issues]


def test_vague_content_negative_passes_meaningful() -> None:
    """A unit with at least MIN_MEANINGFUL_TOKENS meaningful tokens is not vague."""
    unit = _unit(
        title="Detailed decision",
        content=(
            "We migrated retrieval from graph-only to graph plus FTS fusion after "
            "evaluating recall precision on the eval corpus."
        ),
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert not any("vague content (insufficient signal)" in issue.reason for issue in issues)


def test_vague_content_negative_passes_concise_substantive_claim() -> None:
    """A short but substantive claim (e.g. an agent-extracted decision) is not vague.

    Regression guard: the agent-as-provider flow produces deliberately concise
    units. A crisp decision like "Adopt WAL mode for all connections" carries
    only ~3 meaningful tokens but is real durable knowledge — it must NOT be
    flagged as vague, or agent units could never be promoted.
    """
    unit = _unit(
        title="Use SQLite WAL",
        content="Adopt WAL mode for all connections.",
    )
    unit.id = 1
    issues, _flags = validate_units([unit])
    assert not any("vague content (insufficient signal)" in issue.reason for issue in issues)


def test_stale_path_extended_to_non_document(tmp_path: Path) -> None:
    """Absolute-path stale check now applies to non-DOCUMENT source kinds too."""
    missing_dir = tmp_path / "missing-history-fixture"
    missing_path = str(missing_dir / f"nonexistent-{uuid4().hex}.txt")
    non_document_unit = _unit(
        title="History with missing file",
        content="History source pointing at a deleted local file.",
        sources=[_source(missing_path, MemorySourceKind.HISTORY_MESSAGE)],
    )
    non_document_unit.id = 1
    issues, _flags = validate_units([non_document_unit])
    assert any(issue.reason == f"stale source path: {missing_path}" for issue in issues)

    present_path = tmp_path / "exists.txt"
    present_path.write_text("hi")
    present_unit = _unit(
        title="History with present file",
        content="History source pointing at a file that still exists.",
        sources=[_source(str(present_path), MemorySourceKind.HISTORY_MESSAGE)],
    )
    present_unit.id = 2
    issues_present, _flags_present = validate_units([present_unit])
    assert not any("stale source path" in issue.reason for issue in issues_present)


def test_consolidate_summary_counts_promoted_and_rejected(tmp_path: Path) -> None:
    """CLI dry-run output includes a promoted/rejected summary with reason counts."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    promotable_id = store.create_memory_unit(
        _unit(
            title="Confirmed promotion",
            content="Owner confirmed this decision with explicit user confirmation.",
            confidence=0.90,
            sources=[_source("history:promote", MemorySourceKind.HISTORY_MESSAGE)],
        )
    )
    rejected_id = store.create_memory_unit(
        _unit(
            title="Thanks",
            content="ok thanks",
        )
    )

    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "summary:" in result.output
    assert "promoted 1" in result.output
    assert "rejected 1" in result.output
    assert "flagged 0" in result.output
    assert "vague content" in result.output
    promotable_unit = store.get_memory_unit(promotable_id)
    assert promotable_unit is not None
    assert promotable_unit.status == MemoryUnitStatus.ACTIVE_UNIT
    rejected_unit = store.get_memory_unit(rejected_id)
    assert rejected_unit is not None
    assert rejected_unit.status == MemoryUnitStatus.ACTIVE_UNIT


# ---------------------------------------------------------------------------
# M4 Part A2 — Agent/LLM-extracted rationale gate (LIVE) + heuristic FLAG
# ---------------------------------------------------------------------------


def test_agent_extracted_decision_missing_why_is_gated() -> None:
    """An agent/LLM-extracted DECISION with no why goes into validator_rejections."""
    unit = _unit(
        title="Pick database",
        content="Owner picked SQLite for local-first durability and zero ops.",
        unit_type=MemoryUnitType.DECISION,
        why=None,
        sources=[_source("doc:picks", MemorySourceKind.DOCUMENT)],
    )
    unit.id = 1
    rejections, flags = validate_units([unit])
    reasons = [issue.reason for issue in rejections]
    assert "decision missing rationale (why)" in reasons
    # The same unit must NOT also be flagged (gate is mutually exclusive).
    assert all(issue.unit_id != 1 for issue in flags)


def test_heuristic_decision_missing_why_is_flagged_only() -> None:
    """A heuristic (HISTORY_MESSAGE-source) DECISION missing why is advisory, not gated."""
    unit = _unit(
        title="Heuristic decision",
        content="Heuristic checkpoint produced this claim from a history message.",
        unit_type=MemoryUnitType.DECISION,
        why=None,
        sources=[_source("history:msg-1", MemorySourceKind.HISTORY_MESSAGE)],
    )
    unit.id = 1
    rejections, flags = validate_units([unit])
    assert all(issue.unit_id != 1 for issue in rejections)
    flag_reasons = [issue.reason for issue in flags]
    assert "decision missing rationale (why)" in flag_reasons


def test_agent_procedure_missing_how_to_apply_is_gated() -> None:
    """An agent-extracted PROCEDURE missing how_to_apply is rejected."""
    unit = _unit(
        title="Run the migration",
        content=(
            "We run the migration script before opening the store to keep the "
            "schema versioned across the developer team."
        ),
        unit_type=MemoryUnitType.PROCEDURE,
        why="we want durable rationale",
        how_to_apply=None,
        sources=[_source("doc:proc", MemorySourceKind.DOCUMENT)],
    )
    unit.id = 1
    rejections, flags = validate_units([unit])
    reasons = [issue.reason for issue in rejections]
    assert "procedure missing how_to_apply" in reasons
    assert all(issue.unit_id != 1 for issue in flags)


def test_agent_procedure_plan_missing_next_step_is_gated() -> None:
    """Agent-extracted PROCEDURE/PLAN missing next_step is rejected."""
    proc = _unit(
        title="Procedure without next",
        content=(
            "We followed the procedure to add a migration for the new columns. "
            "The team adopted the workflow across the engineering org."
        ),
        unit_type=MemoryUnitType.PROCEDURE,
        why="rationale",
        how_to_apply="recipe",
        next_step=None,
        sources=[_source("doc:proc", MemorySourceKind.DOCUMENT)],
    )
    proc.id = 1
    plan_unit = _unit(
        title="Plan without next",
        content=(
            "The plan covered the rollout strategy, key milestones, and the "
            "operational readiness checklist the team agreed on."
        ),
        unit_type=MemoryUnitType.PLAN,
        why="rationale",
        next_step=None,
        sources=[_source("doc:plan", MemorySourceKind.DOCUMENT)],
    )
    plan_unit.id = 2
    rejections, flags = validate_units([proc, plan_unit])
    reasons = [issue.reason for issue in rejections]
    assert "procedure/plan missing next_step" in reasons
    assert all(issue.unit_id not in (1, 2) for issue in flags)


def test_unit_with_full_rationale_is_neither_gated_nor_flagged() -> None:
    """A unit with why/how/next set is clean (no gate, no flag)."""
    unit = _unit(
        title="Clean decision",
        content=(
            "We chose SQLite for local-first durability and zero operational "
            "footprint across the developer team."
        ),
        unit_type=MemoryUnitType.DECISION,
        why="because trade-offs were reviewed",
        sources=[_source("doc:clean", MemorySourceKind.DOCUMENT)],
    )
    unit.id = 1
    rejections, flags = validate_units([unit])
    assert all(issue.unit_id != 1 for issue in rejections)
    assert all(issue.unit_id != 1 for issue in flags)


def test_consolidation_plan_exposes_validator_flags_for_heuristic_units(tmp_path: Path) -> None:
    """build_consolidation_plan populates validator_flags for heuristic-only why-issues."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    # Heuristic decision — should appear in flags, not in rejections, not blocked.
    # Use two history sources so the provenance gate (which is hard) does not
    # also reject it, isolating the rationale-flag behavior under test.
    heuristic_id = store.create_memory_unit(
        _unit(
            title="Heuristic decision",
            content=(
                "Heuristic checkpoint produced this durable claim from a "
                "history message in a previous session."
            ),
            unit_type=MemoryUnitType.DECISION,
            why=None,
            sources=[
                _source("history:msg-x", MemorySourceKind.HISTORY_MESSAGE, quote="msg-x"),
                _source("history:msg-y", MemorySourceKind.HISTORY_MESSAGE, quote="msg-y"),
            ],
        )
    )
    # Agent-extracted decision missing why — must be in rejections and blocked from promotions.
    agent_id = store.create_memory_unit(
        _unit(
            title="Agent decision",
            content=(
                "Agent produced this with full context and durable signal "
                "from the document the agent just read."
            ),
            unit_type=MemoryUnitType.DECISION,
            why=None,
            sources=[
                _source("doc:agent", MemorySourceKind.DOCUMENT, quote="the agent saw this claim")
            ],
        )
    )
    units = store.list_units_for_consolidation(project="membox")
    counts = {
        unit.id: store.count_independent_sources(unit.id) for unit in units if unit.id is not None
    }
    plan = build_consolidation_plan(units, counts)

    flag_ids = {issue.unit_id for issue in plan.validator_flags}
    rejection_ids = {issue.unit_id for issue in plan.validator_rejections}
    assert heuristic_id in flag_ids
    assert agent_id in rejection_ids
    # Flagged heuristic must remain eligible — does NOT remove from eligible pool.
    assert heuristic_id not in rejection_ids
    # build_consolidation_plan does NOT pass promoted units that were rejected.
    promoted_ids = {transition.unit_id for transition in plan.promotions}
    assert agent_id not in promoted_ids


def test_consolidate_summary_includes_flagged_count(tmp_path: Path) -> None:
    """The CLI summary line shows promoted/rejected/flagged counts."""
    db = str(tmp_path / "memory.db")
    store = KnowledgeStore(db)
    # Heuristic decision with no why → flag.
    store.create_memory_unit(
        _unit(
            title="Heuristic decision",
            content="Heuristic checkpoint produced this from a history message.",
            unit_type=MemoryUnitType.DECISION,
            why=None,
            sources=[_source("history:msg-y", MemorySourceKind.HISTORY_MESSAGE)],
        )
    )
    result = runner.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", "membox", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "flagged 1" in result.output
    assert "decision missing rationale (why)" in result.output
