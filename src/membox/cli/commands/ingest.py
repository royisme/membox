"""`membox ingest` and `membox ingest-file` commands.

Spec §3.9: ingestion is asynchronous by default — the command enqueues the
document (a single SQLite INSERT, no LLM calls) and spawns a short-lived
worker subprocess unless one is already alive.  `--sync` restores the old
blocking behavior; `--no-spawn` enqueues without starting a worker.
"""

from __future__ import annotations

from pathlib import Path

import typer

from membox.cli._common import console, make_agent
from membox.model.schema import IngestMetadata

_SYNC_HELP = "Block until ingestion completes (enqueue + drain inline)."
_NO_SPAWN_HELP = "Enqueue only; do not spawn a worker (start one with `membox process`)."


def _finish_enqueue(db: str, queue_id: int, pending: int, no_spawn: bool) -> None:
    """Print the enqueue receipt and spawn a worker unless suppressed.

    Args:
        db: Database path the worker should drain.
        queue_id: Queue row id returned by the enqueue call.
        pending: Pending + processing count after the enqueue.
        no_spawn: When True, skip worker spawn.
    """
    spawned = False
    if not no_spawn:
        from membox.core.worker import spawn_worker

        spawned = spawn_worker(db)
    note = "worker spawned" if spawned else ("no worker spawned" if no_spawn else "worker alive")
    console.print(f"[green]Enqueued #{queue_id}[/green] — {pending} pending ({note}).")


def ingest(
    text: str = typer.Argument(..., help="Text to ingest into the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    source: str = typer.Option("", "--source", help="Source identifier (file path, URL, etc.)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
    sync: bool = typer.Option(False, "--sync", help=_SYNC_HELP),
    no_spawn: bool = typer.Option(False, "--no-spawn", help=_NO_SPAWN_HELP),
) -> None:
    """Ingest text into the knowledge graph (asynchronously by default)."""
    agent = make_agent(db, no_llm=no_llm, warn=sync)
    if sync:
        agent.ingest(text, source)
        console.print("[green]Ingested.[/green]")
        return
    queue_id = agent.enqueue(text, source_path=source or None)
    _finish_enqueue(db, queue_id, agent.store.pending_ingest_count(), no_spawn)


def ingest_file(
    file: Path = typer.Argument(..., help="Path to file to ingest"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Repository / project name for scoping.  When omitted, the name is "
            "inferred from the nearest git repository root (walking up from the "
            "file's directory), so 'docs/HANDOFF.md' maps to the repo name, not "
            "'docs'.  Falls back to the file's parent directory name if no git "
            "root is found."
        ),
    ),
    doc_date: str | None = typer.Option(
        None,
        "--doc-date",
        help=(
            "ISO-8601 date of the document snapshot (e.g. 2026-06-09).  "
            "Defaults to the file's last-modified date when omitted."
        ),
    ),
    sync: bool = typer.Option(False, "--sync", help=_SYNC_HELP),
    no_spawn: bool = typer.Option(False, "--no-spawn", help=_NO_SPAWN_HELP),
) -> None:
    """Ingest a file into the knowledge graph (asynchronously by default).

    The default path enqueues the file content and returns immediately;
    chunking and LLM extraction happen in a short-lived worker subprocess.
    With --sync, markdown files (.md / .markdown) are chunked on ## section
    boundaries and ingested inline before the command returns.  Re-ingesting
    the same file creates a new version of each document row without deleting
    prior evidence.
    """
    if not file.exists():
        typer.echo(f"Error: file not found: {file}", err=True)
        raise typer.Exit(1)
    agent = make_agent(db, no_llm=no_llm, warn=sync)
    metadata = IngestMetadata(project=project, doc_date=doc_date)
    if sync:
        results = agent.ingest_file(file, metadata=metadata)
        chunk_count = len(results)
        console.print(f"[green]Ingested {file} — {chunk_count} chunk(s).[/green]", soft_wrap=False)
        return
    queue_id = agent.enqueue_file(file, metadata=metadata)
    _finish_enqueue(db, queue_id, agent.store.pending_ingest_count(), no_spawn)
