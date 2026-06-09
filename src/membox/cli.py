"""membox CLI — command-line interface for coding agents."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from membox.agent import MemoryAgent
from membox.extract import DummyExtractor

app = typer.Typer(
    name="membox",
    help="Local knowledge graph + RAG memory layer for coding agents.",
    no_args_is_help=True,
)

_console = Console()


def _make_agent(db: str) -> MemoryAgent:
    """Create a MemoryAgent backed by DummyExtractor for CLI use."""
    return MemoryAgent(extractor=DummyExtractor(), embedder=None, db_path=db)


@app.command()
def version() -> None:
    """Show membox version."""
    from membox import __version__

    typer.echo(f"membox {__version__}")


@app.command()
def ingest(
    text: str = typer.Argument(..., help="Text to ingest into the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    source: str = typer.Option("", "--source", help="Source identifier (file path, URL, etc.)"),
) -> None:
    """Ingest text into the knowledge graph."""
    agent = _make_agent(db)
    agent.ingest(text, source)
    _console.print("[green]Ingested.[/green]")


@app.command("ingest-file")
def ingest_file(
    file: Path = typer.Argument(..., help="Path to file to ingest"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """Ingest a file into the knowledge graph."""
    if not file.exists():
        typer.echo(f"Error: file not found: {file}", err=True)
        raise typer.Exit(1)
    agent = _make_agent(db)
    content = file.read_text(encoding="utf-8")
    agent.ingest(content, source=str(file))
    _console.print(f"[green]Ingested {file}[/green]")


@app.command()
def query(
    question: str = typer.Argument(..., help="Question to query against the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    max_hops: int = typer.Option(2, "--max-hops", help="Maximum BFS hops from seed entities"),
) -> None:
    """Query the knowledge graph and print a structured context."""
    agent = _make_agent(db)
    result = agent.query(question, max_hops)
    typer.echo(result)


@app.command("list-entities")
def list_entities(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """List all entities in the knowledge graph."""
    agent = _make_agent(db)
    entities = agent.list_entities()
    table = Table(title="Entities")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Type")
    for entity in entities:
        table.add_row(str(entity.id), entity.name, entity.type)
    _console.print(table)


@app.command("list-relations")
def list_relations(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """List all relations in the knowledge graph."""
    agent = _make_agent(db)
    relations = agent.list_relations()
    table = Table(title="Relations")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Source", style="green")
    table.add_column("Predicate", style="yellow")
    table.add_column("Target", style="green")
    for rel in relations:
        table.add_row(str(rel.id), rel.source_name, rel.predicate, rel.target_name)
    _console.print(table)
