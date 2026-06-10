"""`membox ingest` and `membox ingest-file` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from membox.cli._common import console, make_agent


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
) -> None:
    """Ingest a file into the knowledge graph."""
    if not file.exists():
        typer.echo(f"Error: file not found: {file}", err=True)
        raise typer.Exit(1)
    agent = make_agent(db, no_llm=no_llm, warn=True)
    content = file.read_text(encoding="utf-8")
    agent.ingest(content, source=str(file))
    console.print(f"[green]Ingested {file}[/green]")
