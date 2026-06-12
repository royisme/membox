"""`membox memory` commands for lifecycle Phase C units."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import typer

from membox.cli._common import console
from membox.core.agent import _infer_project
from membox.core.consolidate import (
    ConsolidationPlan,
    ConsolidationTransition,
    build_consolidation_plan,
)
from membox.core.store import KnowledgeStore
from membox.core.triage import GATE_VERSION, activation_passes, triage_trace
from membox.model.schema import (
    HistoryTriageRecord,
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    TraceKind,
)

memory_app = typer.Typer(
    name="memory",
    help="Triage history trace and manage memory units.",
    no_args_is_help=True,
)

_DB_OPTION = typer.Option("memory.db", "--db", help="Path to SQLite database file")


def _default_project(project: str | None) -> str:
    """Return explicit project or infer it from the current working directory."""
    return project or _infer_project(Path.cwd() / "_")


def _take_lease(store: KnowledgeStore, project: str) -> None:
    """Acquire the lifecycle lease or exit with a clear message."""
    if not store.acquire_lifecycle_lease(project):
        typer.echo(f"Error: another memory apply is running for project {project!r}", err=True)
        raise typer.Exit(1)


@memory_app.command("triage")
def memory_triage(
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 created_at lower bound"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview decisions without writing"),
    apply: bool = typer.Option(False, "--apply", help="Persist triage decisions"),
    limit: int = typer.Option(100, "--limit", help="Maximum trace rows"),
    db: str = _DB_OPTION,
) -> None:
    """Run deterministic triage over imported history trace."""
    if dry_run == apply:
        typer.echo("Error: pass exactly one of --dry-run or --apply", err=True)
        raise typer.Exit(1)
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    rows = store.trace_rows_for_triage(project=effective_project, since=since, limit=limit)
    if apply:
        _take_lease(store, effective_project)
    written = 0
    try:
        for row in rows:
            decision = triage_trace(row["text"], role=row["role"], trace_kind=row["trace_kind"])
            if dry_run:
                typer.echo(
                    f"{row['trace_kind']} {row['trace_id']} "
                    f"extract={decision.should_extract} type={decision.unit_type.value} "
                    f"importance={decision.importance_score:.2f} "
                    f"confidence={decision.confidence_score:.2f} reason={decision.reason}"
                )
                continue
            store.upsert_history_triage(
                HistoryTriageRecord(
                    project=effective_project,
                    trace_kind=TraceKind(row["trace_kind"]),
                    trace_id=row["trace_id"],
                    should_extract=decision.should_extract,
                    unit_type=decision.unit_type,
                    importance_score=decision.importance_score,
                    confidence_score=decision.confidence_score,
                    temporal_type=decision.temporal_type,
                    user_intent=decision.user_intent,
                    extraction_hint=decision.extraction_hint,
                    reason=decision.reason,
                    gate_version=decision.gate_version,
                )
            )
            written += 1
    finally:
        if apply:
            store.release_lifecycle_lease(effective_project)
    console.print(
        f"[green]{'Would triage' if dry_run else 'Triaged'}[/green] {len(rows)} trace rows."
    )
    if apply:
        console.print(f"[green]Wrote[/green] {written} triage rows.")


@memory_app.command("extract")
def memory_extract(
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview units without writing"),
    apply: bool = typer.Option(False, "--apply", help="Write units and consume triage rows"),
    limit: int = typer.Option(100, "--limit", help="Maximum triage rows"),
    db: str = _DB_OPTION,
) -> None:
    """Create deterministic memory-unit candidates from pending triage rows."""
    if dry_run == apply:
        typer.echo("Error: pass exactly one of --dry-run or --apply", err=True)
        raise typer.Exit(1)
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    triage_rows = store.pending_triage_rows(
        project=effective_project, gate_version=GATE_VERSION, limit=limit
    )
    if apply:
        _take_lease(store, effective_project)
    created = 0
    consumed: list[int] = []
    try:
        for triage in triage_rows:
            trace = store.get_trace_text(triage.trace_kind.value, triage.trace_id)
            if trace is None:
                typer.echo(f"skip missing source {triage.trace_kind.value}:{triage.trace_id}")
                continue
            decision = triage_trace(
                trace["text"], role=trace["role"], trace_kind=trace["trace_kind"]
            )
            unit = _unit_from_trace(effective_project, trace, decision)
            if dry_run:
                typer.echo(
                    f"create title={unit.title!r} type={unit.unit_type.value} "
                    f"status={unit.status.value} labels={','.join(unit.labels)}"
                )
                continue
            store.create_memory_unit(unit)
            if triage.id is not None:
                consumed.append(triage.id)
            created += 1
        if apply:
            store.mark_triage_consumed(consumed)
    finally:
        if apply:
            store.release_lifecycle_lease(effective_project)
    console.print(f"[green]{'Would create' if dry_run else 'Created'}[/green] {created} units.")


@memory_app.command("consolidate")
def memory_consolidate(
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 updated_at lower bound"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview consolidation without writing"),
    apply: bool = typer.Option(False, "--apply", help="Apply consolidation transitions"),
    limit: int = typer.Option(500, "--limit", help="Maximum units to inspect"),
    db: str = _DB_OPTION,
) -> None:
    """Promote crystals, surface conflicts, supersede stale units, and run decay."""
    if dry_run == apply:
        typer.echo("Error: pass exactly one of --dry-run or --apply", err=True)
        raise typer.Exit(1)
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    units = store.list_units_for_consolidation(
        project=effective_project,
        since=since,
        limit=limit,
    )
    counts = store.count_independent_sources_for_units(
        [unit.id for unit in units if unit.id is not None]
    )
    plan = build_consolidation_plan(units, counts)
    _print_consolidation_plan(plan, dry_run=dry_run)
    if dry_run:
        return
    _take_lease(store, effective_project)
    applied = 0
    try:
        for action in _ordered_transitions(plan):
            ok = store.transition_memory_unit(
                action.unit_id,
                action.to_status,
                command="memory consolidate",
                reason=action.reason,
                superseded_by=action.superseded_by,
            )
            applied += int(ok)
    except Exception:
        console.print(f"[red]Aborted[/red] after applying {applied} consolidation transitions.")
        raise
    finally:
        store.release_lifecycle_lease(effective_project)
    console.print(f"[green]Applied[/green] {applied} consolidation transitions.")


@memory_app.command("list")
def memory_list(
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    status: str | None = typer.Option(None, "--status", help="Filter by unit status"),
    limit: int = typer.Option(50, "--limit", help="Maximum rows"),
    db: str = _DB_OPTION,
) -> None:
    """List memory units."""
    store = KnowledgeStore(db)
    units = store.list_memory_units(project=_default_project(project), status=status, limit=limit)
    if not units:
        typer.echo("No memory units.")
        return
    for unit in units:
        typer.echo(f"{unit.id} {unit.status.value} {unit.unit_type.value} {unit.title}")


@memory_app.command("show")
def memory_show(
    unit_id: int = typer.Argument(..., help="Memory unit id"),
    db: str = _DB_OPTION,
) -> None:
    """Show one memory unit with labels and sources."""
    store = KnowledgeStore(db)
    unit = store.get_memory_unit(unit_id)
    if unit is None:
        typer.echo(f"Error: no such unit: {unit_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{unit.id} {unit.status.value} {unit.unit_type.value}")
    typer.echo(unit.title)
    typer.echo(unit.content)
    if unit.labels:
        typer.echo("labels: " + ", ".join(unit.labels))
    for source in unit.sources:
        typer.echo(
            f"source: {source.source_kind.value}:{source.source_ref}"
            f" message={source.source_message_id}"
        )


@memory_app.command("retract")
def memory_retract(
    unit_id: int = typer.Argument(..., help="Memory unit id"),
    reason: str = typer.Option("", "--reason", help="Audit reason"),
    project: str | None = typer.Option(None, "--project", help="Project scope for lease"),
    db: str = _DB_OPTION,
) -> None:
    """Retract a memory unit."""
    _transition(unit_id, MemoryUnitStatus.RETRACTED, "memory retract", reason, project, db)


@memory_app.command("restore")
def memory_restore(
    unit_id: int = typer.Argument(..., help="Memory unit id"),
    reason: str = typer.Option("", "--reason", help="Audit reason"),
    project: str | None = typer.Option(None, "--project", help="Project scope for lease"),
    db: str = _DB_OPTION,
) -> None:
    """Restore an archived memory unit to its prior status."""
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    _take_lease(store, effective_project)
    try:
        ok = store.restore_memory_unit(unit_id, reason=reason)
    finally:
        store.release_lifecycle_lease(effective_project)
    if not ok:
        typer.echo(f"Error: no such unit: {unit_id}", err=True)
        raise typer.Exit(1)
    console.print(f"[green]Restored[/green] {unit_id}.")


@memory_app.command("supersede")
def memory_supersede(
    old_id: int = typer.Argument(..., help="Old unit id"),
    new_id: int = typer.Argument(..., help="Replacement unit id"),
    reason: str = typer.Option("", "--reason", help="Audit reason"),
    project: str | None = typer.Option(None, "--project", help="Project scope for lease"),
    db: str = _DB_OPTION,
) -> None:
    """Mark one unit as superseded by another."""
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    _take_lease(store, effective_project)
    try:
        ok = store.transition_memory_unit(
            old_id,
            MemoryUnitStatus.SUPERSEDED,
            command="memory supersede",
            reason=reason,
            superseded_by=new_id,
        )
    finally:
        store.release_lifecycle_lease(effective_project)
    if not ok:
        typer.echo(f"Error: no such unit: {old_id}", err=True)
        raise typer.Exit(1)
    console.print(f"[green]Superseded[/green] {old_id} -> {new_id}.")


def _transition(
    unit_id: int,
    status: MemoryUnitStatus,
    command: str,
    reason: str,
    project: str | None,
    db: str,
) -> None:
    """Run a leased status transition."""
    effective_project = _default_project(project)
    store = KnowledgeStore(db)
    _take_lease(store, effective_project)
    try:
        ok = store.transition_memory_unit(unit_id, status, command=command, reason=reason)
    finally:
        store.release_lifecycle_lease(effective_project)
    if not ok:
        typer.echo(f"Error: no such unit: {unit_id}", err=True)
        raise typer.Exit(1)
    console.print(f"[green]Updated[/green] {unit_id} -> {status.value}.")


def _ordered_transitions(plan: ConsolidationPlan) -> list[ConsolidationTransition]:
    """Return transitions in a deterministic apply order."""
    return [
        *plan.supersessions,
        *plan.decay_archives,
        *plan.promotions,
        *plan.candidates,
        *plan.demotions,
    ]


def _print_consolidation_plan(plan: ConsolidationPlan, *, dry_run: bool) -> None:
    """Render a consolidation plan as script-friendly lines."""
    prefix = "would " if dry_run else ""
    for issue in plan.validator_rejections:
        typer.echo(f"validator reject {issue.unit_id} title={issue.title!r} reason={issue.reason}")
    for conflict in plan.conflicts:
        typer.echo(
            f"conflict review {conflict.left_id}<->{conflict.right_id} "
            f"left={conflict.left_title!r} right={conflict.right_title!r} "
            f"reason={conflict.reason} sources={','.join(conflict.source_refs)}"
        )
    for issue in plan.decay_reviews:
        typer.echo(f"decay review {issue.unit_id} title={issue.title!r} reason={issue.reason}")
    for group_name, transitions in (
        ("supersede", plan.supersessions),
        ("archive", plan.decay_archives),
        ("promote", plan.promotions),
        ("candidate", plan.candidates),
        ("demote", plan.demotions),
    ):
        for action in transitions:
            target = (
                f" superseded_by={action.superseded_by}" if action.superseded_by is not None else ""
            )
            typer.echo(
                f"{prefix}{group_name} {action.unit_id} -> {action.to_status.value}"
                f"{target} title={action.title!r} reason={action.reason}"
            )
    transition_count = len(_ordered_transitions(plan))
    conflict_count = len(plan.conflicts)
    issue_count = len(plan.validator_rejections) + len(plan.decay_reviews)
    console.print(
        f"[green]{'Would apply' if dry_run else 'Planned'}[/green] "
        f"{transition_count} transitions, {conflict_count} conflicts, {issue_count} issues."
    )


def _unit_from_trace(
    project: str,
    trace: Mapping[str, object],
    decision: object,
) -> MemoryUnitRecord:
    """Build a deterministic Phase C unit from one trace row."""
    from membox.core.triage import GateDecision

    typed_decision = decision
    assert isinstance(typed_decision, GateDecision)
    source_kind = (
        MemorySourceKind.HISTORY_MESSAGE
        if trace["trace_kind"] == "message"
        else MemorySourceKind.HISTORY_EVENT
    )
    text = str(trace["text"]).strip()
    source = MemoryUnitSource(
        source_kind=source_kind,
        source_ref=str(trace["trace_id"]),
        source_message_id=str(trace["trace_id"]) if trace["trace_kind"] == "message" else "",
        quote=text[:300],
    )
    status = (
        MemoryUnitStatus.ACTIVE_UNIT
        if activation_passes(typed_decision, has_source=True)
        else MemoryUnitStatus.UNIT_CANDIDATE
    )
    return MemoryUnitRecord(
        project=project,
        unit_type=typed_decision.unit_type,
        status=status,
        title=typed_decision.extraction_hint or "Memory unit",
        content=text[:1200],
        context=f"Extracted from {trace['trace_kind']} {trace['trace_id']}",
        importance_score=typed_decision.importance_score,
        confidence_score=typed_decision.confidence_score,
        temporal_type=typed_decision.temporal_type,
        labels=typed_decision.labels,
        sources=[source],
    )
