"""`membox query` command."""

from __future__ import annotations

import typer

from membox.cli._common import make_agent


def query(
    question: str = typer.Argument(..., help="Question to query against the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    max_hops: int = typer.Option(2, "--max-hops", help="Maximum BFS hops from seed entities"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
    budget: int | None = typer.Option(
        None,
        "--budget",
        help=(
            "Token budget for compact output (spec §3.7 scoring + knapsack truncation). "
            "Omit to use the legacy prompt-context format."
        ),
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Filter evidence to this project name only.",
    ),
) -> None:
    """Query the knowledge graph and print a structured context.

    When --budget is supplied the output uses the compact subject-grouped
    format with token-budget truncation and a coverage footer.  Without
    --budget the legacy knowledge-topology format is used.
    """
    agent = make_agent(db, no_llm=no_llm, warn=True)
    result = agent.query(question, max_hops=max_hops, budget=budget, project_filter=project)
    typer.echo(result)
