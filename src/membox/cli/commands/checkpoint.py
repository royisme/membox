"""``membox checkpoint`` — one-shot lifecycle capture (pull → triage → extract).

Thin CLI shell: parse options, call :func:`~membox.core.lifecycle.run_checkpoint`,
print the summary. The orchestration lives in the service layer so the v0.2
hook-driven trigger can call the same function without diverging from the
manual flow.

Lease conflict, malformed JSONL, and empty results each have their own
summary line so the exit code carries meaning:

- ``0`` — success or expected empty state (no new sessions; 0 units is fine).
- ``1`` — lease conflict ("another lifecycle operation is in progress")
  **or** malformed JSONL during pull (existing ``history pull`` semantics).
"""

from __future__ import annotations

from pathlib import Path

import typer

from membox.cli.commands.history import _resolve_session_root
from membox.cli.commands.memory import _DB_OPTION, _default_project
from membox.core.lifecycle import CheckpointResult, LifecycleLeaseError, run_checkpoint
from membox.core.store import KnowledgeStore
from membox.services.importers import IMPORTER_FORMATS


def _render_summary(result: CheckpointResult) -> str:
    """Render the one-line summary for ``result``.

    Uses the EXACT strings from the spec "Output" section. Empty cases
    read as expected, never as failure: a separate caller check decides
    exit code based on ``skipped_lines`` alone.
    """
    if result.sessions_pulled == 0 and result.messages_pulled == 0:
        return "checkpoint: nothing new to capture since last checkpoint"
    if result.triaged_rows > 0 and result.units_created == 0:
        return (
            f"checkpoint: captured {result.triaged_rows} traces; "
            "0 met the extraction bar (no durable decisions/fixes this session) "
            "— this is expected"
        )
    if result.applied:
        prefix = "✓ checkpoint: "
        pull_v, triage_v, extract_v = "pulled", "triaged", "extracted"
    else:
        # Dry-run: same shape, every verb in base form prefixed with "would".
        prefix = ""
        pull_v, triage_v, extract_v = "would pull", "would triage", "would extract"
    return (
        f"{prefix}{pull_v} {result.sessions_pulled} sessions "
        f"({result.messages_pulled} msgs) → {triage_v} {result.triaged_rows} "
        f"→ {extract_v} {result.units_created} units"
    )


def checkpoint(
    adapt: str = typer.Option(
        "membox-capture",
        "--adapt",
        help=f"Agent adapter: {', '.join(sorted(IMPORTER_FORMATS))}",
    ),
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    session_root: str | None = typer.Option(
        None,
        "--session-root",
        help="Agent session storage root (default: $MEMBOX_SESSION_ROOT)",
    ),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 created_at lower bound"),
    limit: int = typer.Option(100, "--limit", help="Maximum trace rows for triage/extract"),
    apply: bool = typer.Option(
        True,
        "--apply/--dry-run",
        help="Apply changes (default) or preview without writing",
    ),
    db: str = _DB_OPTION,
) -> None:
    """Run a one-shot lifecycle checkpoint: pull → triage → extract.

    By default applies changes to the database. ``--dry-run`` runs the
    identical sequence against an ephemeral store and prints what the
    apply run would have produced.
    """
    if adapt not in IMPORTER_FORMATS:
        known = ", ".join(sorted(IMPORTER_FORMATS))
        typer.echo(f"Error: unknown adapter {adapt!r}; known: {known}", err=True)
        raise typer.Exit(1)

    effective_project = _default_project(project)
    resolved_root = _resolve_session_root(session_root)
    if resolved_root is None:
        # Allow pull to report "0 sessions" naturally instead of erroring —
        # an empty session root simply means "no discovery", which still
        # yields the nothing-new summary.
        resolved_root_path: Path | None = None
    else:
        resolved_root_path = resolved_root

    store = KnowledgeStore(db)
    try:
        result = run_checkpoint(
            store,
            project=effective_project,
            session_root=str(resolved_root_path) if resolved_root_path is not None else None,
            adapt=adapt,
            since=since,
            limit=limit,
            apply=apply,
        )
    except LifecycleLeaseError:
        typer.echo("Error: another lifecycle operation is in progress", err=True)
        raise typer.Exit(1) from None

    typer.echo(_render_summary(result))
    if result.skipped_lines > 0:
        typer.echo(f"{result.skipped_lines} malformed lines skipped", err=True)
        raise typer.Exit(1)
