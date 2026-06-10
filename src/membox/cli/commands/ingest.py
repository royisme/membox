"""`membox ingest` and `membox ingest-file` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from membox.cli._common import console, make_agent
from membox.model.schema import IngestMetadata


def ingest(
    text: str = typer.Argument(..., help="Text to ingest into the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    source: str = typer.Option("", "--source", help="Source identifier (file path, URL, etc.)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
) -> None:
    """Ingest text into the knowledge graph."""
    agent = make_agent(db, no_llm=no_llm, warn=True)
    agent.ingest(text, source)
    console.print("[green]Ingested.[/green]")


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
) -> None:
    """Ingest a file into the knowledge graph.

    Markdown files (.md / .markdown) are chunked on ## section boundaries;
    each section is ingested as a separate document row carrying the section
    heading and document metadata.  Re-ingesting the same file creates a new
    version of each document row without deleting prior evidence.
    """
    if not file.exists():
        typer.echo(f"Error: file not found: {file}", err=True)
        raise typer.Exit(1)
    agent = make_agent(db, no_llm=no_llm, warn=True)
    metadata = IngestMetadata(project=project, doc_date=doc_date)
    results = agent.ingest_file(file, metadata=metadata)
    chunk_count = len(results)
    console.print(f"[green]Ingested {file} — {chunk_count} chunk(s).[/green]")
