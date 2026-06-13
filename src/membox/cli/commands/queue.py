"""`membox process` and `membox queue` commands (M6 async ingestion)."""

from __future__ import annotations

import typer
from rich.table import Table

from membox.cli._common import console, make_agent


def process(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Reset retryable failed rows (retries < 3) to pending before draining.",
    ),
) -> None:
    """Drain the ingest queue, then exit (no daemon).

    Claims pending rows one at a time, runs the chunk → extract → embed →
    store pipeline on each, and exits when the queue is empty.  A live worker
    lease held by another process makes this a no-op.
    """
    from membox.core.worker import drain_queue

    agent = make_agent(db, no_llm=no_llm)
    stats = drain_queue(agent, retry_failed=retry_failed)
    console.print(
        f"Processed: [green]{stats['done']} done[/green], "
        f"[red]{stats['failed']} failed[/red], {stats['retried']} retried."
    )


def queue_status(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """Show ingest queue status: per-status counts and recent failures."""
    agent = make_agent(db, no_llm=True)
    counts = agent.store.queue_counts()

    table = Table(title="Ingest Queue")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status in ("pending", "processing", "done", "failed"):
        table.add_row(status, str(counts[status]))
    console.print(table)

    failures = agent.store.recent_failures()
    if failures:
        ftable = Table(title="Recent Failures")
        ftable.add_column("ID", justify="right")
        ftable.add_column("Source")
        ftable.add_column("Retries", justify="right")
        ftable.add_column("Error")
        for f in failures:
            ftable.add_row(
                str(f["id"]),
                str(f["source_path"] or ""),
                str(f["retries"]),
                str(f["error"] or ""),
            )
        console.print(ftable)
