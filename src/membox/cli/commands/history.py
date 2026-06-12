"""``membox history`` command group — session-trace pull and search.

Presentation only: parsing lives in :mod:`membox.services.importers`, storage
in :mod:`membox.core.store.history`, orchestration in
:mod:`membox.core.history_import`.

Project scoping is a hard invariant from the lifecycle design: search-style
commands default to the current project (inferred from the working
directory's git root, the same resolution ingest uses for files) and require
an explicit ``--all-projects`` flag for cross-project output, so a single
shared database never leaks another project's trace by default.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from membox.cli._common import console
from membox.config import HistoryConfig
from membox.core.agent import _infer_project
from membox.core.history_import import fetch_payload, history_pull, import_history
from membox.core.store import KnowledgeStore
from membox.core.triage import redact_secrets
from membox.services.importers import IMPORTER_FORMATS

history_app = typer.Typer(
    name="history",
    help="Pull and search agent session history (trace layer).",
    no_args_is_help=True,
)

_DB_OPTION = typer.Option("memory.db", "--db", help="Path to SQLite database file")
_PROJECT_HELP = (
    "Project scope.  Defaults to the current project (git root name of the "
    "working directory).  Use --all-projects for cross-project output."
)


def _resolve_project(project: str | None, all_projects: bool) -> str | None:
    """Resolve the effective project filter for a search-style command.

    Args:
        project: Explicit ``--project`` value, if any.
        all_projects: True when ``--all-projects`` was passed.

    Returns:
        Project name to filter on, or None for cross-project.
    """
    if all_projects:
        return None
    if project is not None:
        return project
    # _infer_project walks up from the path's parent, so hand it a synthetic
    # child of the working directory.
    return _infer_project(Path.cwd() / "_")


def _print_hits(rows: list[dict[str, object]], empty_msg: str) -> None:
    """Print generic history rows, one block per row."""
    if not rows:
        typer.echo(empty_msg)
        return
    for row in rows:
        header = " ".join(
            str(row[key])
            for key in ("created_at", "kind", "tool_name", "role", "file_path")
            if key in row and row[key] is not None
        )
        typer.echo(f"--- {row['id']}")
        if header:
            typer.echo(header)
        body = str(row.get("body") or row.get("text") or "")
        typer.echo(body)


@history_app.command("pull")
def history_pull_command(
    format_name: str = typer.Option(
        ...,
        "--adapt",
        help=f"Agent adapter: {', '.join(sorted(IMPORTER_FORMATS))}",
    ),
    project: str | None = typer.Option(None, "--project", help="Project scope override"),
    session_root: str | None = typer.Option(
        None,
        "--session-root",
        help="Agent session storage root (default: $MEMBOX_SESSION_ROOT)",
    ),
    path: Path | None = typer.Argument(None, help="Single session file to import (optional)"),
    db: str = _DB_OPTION,
) -> None:
    """Pull session history from an agent adapter (auto-discovery or single file).

    Without a path argument, auto-discovers sessions matching the current
    working directory under --session-root (or $MEMBOX_SESSION_ROOT).
    With a path, imports that single file directly.
    """
    if format_name not in IMPORTER_FORMATS:
        known = ", ".join(sorted(IMPORTER_FORMATS))
        typer.echo(f"Error: unknown adapter {format_name!r}; known: {known}", err=True)
        raise typer.Exit(1)

    store = KnowledgeStore(db)

    if path is not None:
        # Single-file import (existing behavior).
        if not path.exists():
            typer.echo(f"Error: file not found: {path}", err=True)
            raise typer.Exit(1)
        import_result = import_history(
            store,
            path,
            format_name,
            project=project,
            text_cap_bytes=HistoryConfig().text_cap_bytes,
        )
        if import_result["skipped"]:
            console.print(f"[yellow]Unchanged, skipped:[/yellow] {import_result['session_id']}")
        else:
            console.print(
                f"[green]Imported[/green] {import_result['messages']} messages, "
                f"{import_result['events']} events into session {import_result['session_id']}"
            )
        return

    # Auto-discovery mode.
    resolved_root = _resolve_session_root(session_root)
    if resolved_root is None:
        typer.echo("Error: set MEMBOX_SESSION_ROOT or pass --session-root", err=True)
        raise typer.Exit(1)

    pull_result = history_pull(
        store,
        format_name,
        project=project,
        session_root=resolved_root,
        text_cap_bytes=HistoryConfig().text_cap_bytes,
    )
    if pull_result["sessions"] == 0:
        typer.echo("No sessions found for current project.")
        return
    console.print(
        f"[green]Pulled[/green] {pull_result['messages']} messages, "
        f"{pull_result['events']} events from {pull_result['sessions']} session(s)"
    )


@history_app.command("search")
def history_search(
    query: str = typer.Argument(..., help="Full-text query (CJK-safe)"),
    project: str | None = typer.Option(None, "--project", help=_PROJECT_HELP),
    all_projects: bool = typer.Option(False, "--all-projects", help="Search every project"),
    session: str | None = typer.Option(None, "--session", help="Restrict to one session ID"),
    kind: str | None = typer.Option(
        None, "--kind", help="Event kind filter (tool_call, tool_result, tool_error, …)"
    ),
    tool: str | None = typer.Option(None, "--tool", help="Tool-name filter"),
    file_path: str | None = typer.Option(
        None, "--file", help="Exact file path or directory prefix filter"
    ),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 created_at lower bound"),
    limit: int = typer.Option(20, "--limit", help="Maximum hits"),
    db: str = _DB_OPTION,
) -> None:
    """Search imported history messages and events."""
    store = KnowledgeStore(db)
    hits = store.search_history(
        query,
        project=_resolve_project(project, all_projects),
        session_id=session,
        kind=kind,
        tool=tool,
        file_path=file_path,
        since=since,
        limit=limit,
    )
    if not hits:
        typer.echo("No history hits.")
        return
    for hit in hits:
        flags = " [error]" if hit["is_error"] else ""
        flags += " [truncated]" if hit["truncated"] else ""
        typer.echo(f"--- {hit['id']}")
        typer.echo(
            f"{hit['created_at'] or '(no timestamp)'} {hit['kind']} "
            f"{hit['role_or_tool']} project={hit['project']}{flags}"
        )
        typer.echo(hit["preview"])


@history_app.command("around")
def history_around(
    message_id: str = typer.Argument(..., help="Stable message ID at the window center"),
    project: str | None = typer.Option(None, "--project", help=_PROJECT_HELP),
    all_projects: bool = typer.Option(False, "--all-projects", help="Search every project"),
    radius: int = typer.Option(3, "--radius", help="Messages on each side"),
    db: str = _DB_OPTION,
) -> None:
    """Show the conversation window around one message."""
    store = KnowledgeStore(db)
    rows = store.history_around(
        message_id, radius=radius, project=_resolve_project(project, all_projects)
    )
    if not rows:
        typer.echo(f"Error: no such message: {message_id}", err=True)
        raise typer.Exit(1)
    for row in rows:
        marker = ">>>" if row["id"] == message_id else "   "
        typer.echo(f"{marker} [{row['seq']}] {row['role']} ({row['created_at']})")
        typer.echo(str(row["text"]))


@history_app.command("fetch")
def history_fetch(
    record_id: str = typer.Argument(..., help="Stable message or event ID"),
    project: str | None = typer.Option(None, "--project", help=_PROJECT_HELP),
    all_projects: bool = typer.Option(False, "--all-projects", help="Search every project"),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print raw upstream payload without secret redaction",
    ),
    db: str = _DB_OPTION,
) -> None:
    """Print the full payload re-read from the upstream log (never stored)."""
    store = KnowledgeStore(db)
    result = fetch_payload(store, record_id, project=_resolve_project(project, all_projects))
    if not result["found"]:
        typer.echo(f"Error: {result['note']}", err=True)
        raise typer.Exit(1)
    payload = result["payload"] if raw else redact_secrets(result["payload"])
    typer.echo(payload)


@history_app.command("file")
def history_file(
    file_path: str = typer.Argument(..., help="Exact file path or directory prefix to look up"),
    project: str | None = typer.Option(None, "--project", help=_PROJECT_HELP),
    all_projects: bool = typer.Option(False, "--all-projects", help="Search every project"),
    limit: int = typer.Option(50, "--limit", help="Maximum rows"),
    db: str = _DB_OPTION,
) -> None:
    """Show events that touched a file, newest first."""
    store = KnowledgeStore(db)
    rows = store.history_file(
        file_path, project=_resolve_project(project, all_projects), limit=limit
    )
    _print_hits(rows, "No events for that file.")


@history_app.command("failures")
def history_failures(
    project: str | None = typer.Option(None, "--project", help=_PROJECT_HELP),
    all_projects: bool = typer.Option(False, "--all-projects", help="Search every project"),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 created_at lower bound"),
    limit: int = typer.Option(50, "--limit", help="Maximum rows"),
    db: str = _DB_OPTION,
) -> None:
    """Show failed tool events, newest first."""
    store = KnowledgeStore(db)
    rows = store.history_failures(
        project=_resolve_project(project, all_projects), since=since, limit=limit
    )
    _print_hits(rows, "No failures recorded.")


def _resolve_session_root(explicit: str | None) -> Path | None:
    """Resolve session root from explicit flag or MEMBOX_SESSION_ROOT env var."""
    raw = explicit or os.environ.get("MEMBOX_SESSION_ROOT")
    if raw is None:
        return None
    return Path(raw).expanduser()
