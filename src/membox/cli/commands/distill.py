"""`membox distill` read-only workflow packaging analysis command."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from membox.cli._common import console
from membox.core.distill import (
    DistillCandidate,
    FilesystemAssetInventory,
    build_distill_plan,
)
from membox.core.project import infer_project
from membox.core.store import KnowledgeStore

_DB_OPTION = typer.Option("memory.db", "--db", help="Path to SQLite database file")
_DURATION_RE = re.compile(r"^(\d+)([dwh])$")


def distill(
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Optional updated_at window such as 30d or an ISO-8601 lower bound",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview candidates without writing"),
    apply: bool = typer.Option(False, "--apply", help="Reserved for a later Phase F apply path"),
    root: Path = typer.Option(Path("."), "--root", help="Filesystem root to inventory"),
    limit: int = typer.Option(500, "--limit", help="Maximum eligible units to inspect"),
    db: str = _DB_OPTION,
) -> None:
    """Identify repeated workflows worth packaging; read-only, no lifecycle lease."""
    if apply:
        typer.echo(
            "Error: --apply is not yet implemented; use --dry-run to preview candidates.",
            err=True,
        )
        raise typer.Exit(1)
    if not dry_run:
        typer.echo(
            "Error: --apply is not yet implemented; use --dry-run to preview candidates.",
            err=True,
        )
        raise typer.Exit(1)
    scanned_root = root.expanduser().resolve()
    if not scanned_root.exists():
        typer.echo(f"Error: --root does not exist: {scanned_root}", err=True)
        raise typer.Exit(1)

    effective_project = project or infer_project(scanned_root / "_")
    since_lower_bound = _since_lower_bound(since)
    store = KnowledgeStore(db)
    units = store.list_units_for_distill(
        project=effective_project,
        since=since_lower_bound,
        limit=limit,
    )
    unit_ids = [unit.id for unit in units if unit.id is not None]
    counts = store.count_independent_sources_for_units(unit_ids)
    assets = FilesystemAssetInventory().list_assets(scanned_root)
    plan = build_distill_plan(
        units,
        counts,
        assets=assets,
        independent_source_counter=store.count_independent_sources_for_unit_group,
    )

    typer.echo(f"project: {effective_project}")
    typer.echo(f"window: {_window_label(since)}")
    typer.echo(f"scanned_root: {scanned_root}")
    if not plan.candidates:
        typer.echo(f"no distill candidates found (scanned_units={plan.scanned_unit_count})")
        typer.echo("created nothing")
        return
    for candidate in plan.candidates:
        _print_candidate(candidate)
    console.print(
        f"[green]Found[/green] {len(plan.candidates)} distill candidates "
        f"(scanned_units={plan.scanned_unit_count})."
    )


def _print_candidate(candidate: DistillCandidate) -> None:
    """Print one distill candidate block."""
    typer.echo("")
    typer.echo(f"candidate: {candidate.members[0].title}")
    typer.echo(f"form: {candidate.recommended_form}")
    typer.echo(
        "frequency: "
        f"evidence_sessions={candidate.evidence_sessions}, "
        f"units={candidate.unit_count}, "
        f"recalls={candidate.summed_recall_count}"
    )
    if candidate.covered_by is not None:
        typer.echo(f"covered_by: {candidate.covered_by}")
    typer.echo("members:")
    for member in candidate.members:
        typer.echo(f"  - {member.unit_id} {member.unit_type.value} {member.title}")
    typer.echo(f"explain: {candidate.explain}")


def _since_lower_bound(value: str | None) -> str | None:
    """Convert a CLI window value into a SQLite-comparable timestamp.

    Args:
        value: A duration string such as ``30d``, ``4w``, or ``12h``, or an
            ISO-8601 timestamp string, or ``None`` to return no lower bound.

    Returns:
        An ISO-8601 timestamp string suitable for SQL comparison, or ``None``.

    Raises:
        typer.BadParameter: If *value* is not a recognised duration or a valid
            ISO-8601 timestamp.
    """
    if value is None:
        return None
    match = _DURATION_RE.match(value)
    if match is not None:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "w":
            delta = timedelta(weeks=amount)
        else:  # "h"
            delta = timedelta(hours=amount)
        return (datetime.now(UTC) - delta).isoformat(timespec="seconds")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        msg = (
            f"Invalid --since value {value!r}. "
            "Accepted formats: Nd (days), Nw (weeks), Nh (hours), or an ISO-8601 timestamp."
        )
        raise typer.BadParameter(msg, param_hint="'--since'") from exc
    return value


def _window_label(value: str | None) -> str:
    return "all" if value is None else f"since {value}"
