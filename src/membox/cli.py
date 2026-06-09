"""membox CLI — command-line interface for coding agents."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from membox.agent import MemoryAgent
from membox.extract import DummyExtractor, create_default_extractor

app = typer.Typer(
    name="membox",
    help="Local knowledge graph + RAG memory layer for coding agents.",
    no_args_is_help=True,
)

_console = Console()

_NO_LLM_NOTICE = (
    "No OPENAI_API_KEY / openai package — using no-op extractor; nothing will be extracted."
)


def _make_agent(db: str, no_llm: bool = False, warn: bool = False) -> MemoryAgent:
    """Create a MemoryAgent using the best available extraction backend.

    Args:
        db: Path to the SQLite database file.
        no_llm: Force the no-op Dummy backend even if OpenAI is available.
        warn: Print a notice to stderr when the no-op backend is active.

    Returns:
        Configured MemoryAgent.
    """
    extractor, embedder = create_default_extractor(use_llm=not no_llm)
    if warn and isinstance(extractor, DummyExtractor):
        typer.echo(_NO_LLM_NOTICE, err=True)
    return MemoryAgent(extractor=extractor, embedder=embedder, db_path=db)


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
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
) -> None:
    """Ingest text into the knowledge graph."""
    agent = _make_agent(db, no_llm=no_llm, warn=True)
    agent.ingest(text, source)
    _console.print("[green]Ingested.[/green]")


@app.command("ingest-file")
def ingest_file(
    file: Path = typer.Argument(..., help="Path to file to ingest"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
) -> None:
    """Ingest a file into the knowledge graph."""
    if not file.exists():
        typer.echo(f"Error: file not found: {file}", err=True)
        raise typer.Exit(1)
    agent = _make_agent(db, no_llm=no_llm, warn=True)
    content = file.read_text(encoding="utf-8")
    agent.ingest(content, source=str(file))
    _console.print(f"[green]Ingested {file}[/green]")


@app.command()
def query(
    question: str = typer.Argument(..., help="Question to query against the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    max_hops: int = typer.Option(2, "--max-hops", help="Maximum BFS hops from seed entities"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
) -> None:
    """Query the knowledge graph and print a structured context."""
    agent = _make_agent(db, no_llm=no_llm, warn=True)
    result = agent.query(question, max_hops)
    typer.echo(result)


@app.command("list-entities")
def list_entities(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """List all entities in the knowledge graph."""
    agent = _make_agent(db, no_llm=True)
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
    agent = _make_agent(db, no_llm=True)
    relations = agent.list_relations()
    table = Table(title="Relations")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Source", style="green")
    table.add_column("Predicate", style="yellow")
    table.add_column("Target", style="green")
    for rel in relations:
        table.add_row(str(rel.id), rel.source_name, rel.predicate, rel.target_name)
    _console.print(table)
