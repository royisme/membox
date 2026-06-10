"""`membox query` command."""

from __future__ import annotations

import typer

from membox.cli._common import make_agent


def query(
    question: str = typer.Argument(..., help="Question to query against the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    max_hops: int = typer.Option(2, "--max-hops", help="Maximum BFS hops from seed entities"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
) -> None:
    """Query the knowledge graph and print a structured context."""
    agent = make_agent(db, no_llm=no_llm, warn=True)
    result = agent.query(question, max_hops)
    typer.echo(result)
