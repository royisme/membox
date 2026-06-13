"""`membox query` command."""

from __future__ import annotations

from pathlib import Path

import typer

from membox.cli._common import make_agent
from membox.core.project import infer_project


def query(
    question: str = typer.Argument(..., help="Question to query against the knowledge graph"),
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
    max_hops: int = typer.Option(2, "--max-hops", help="Maximum BFS hops from seed entities"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the no-op extraction backend"),
    budget: int | None = typer.Option(
        None,
        "--budget",
        help=(
            "Token budget override for compact output (spec §3.7 scoring + knapsack "
            "truncation). Defaults to config retrieval.budget (2000) when omitted."
        ),
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Filter evidence to this project name only.",
    ),
    include_superseded: bool = typer.Option(
        False,
        "--include-superseded",
        help="Include superseded (older-version) relations in the query results.",
    ),
    include_memory: bool = typer.Option(
        False,
        "--include-memory",
        help="Include opt-in crystals and memory units in a separate budget partition.",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="When --include-memory is set, recall memory from every project.",
    ),
) -> None:
    """Query the knowledge graph and print a compact context.

    Output always uses the compact subject-grouped format with token-budget
    truncation and a coverage footer (spec §3.7).  Use --budget to override
    the default token budget (config retrieval.budget, 2000).
    Use --include-superseded to expose relations that were superseded by a
    newer version of the same source document.
    """
    agent = make_agent(db, no_llm=no_llm)
    memory_project = None
    if include_memory and not all_projects:
        memory_project = project or infer_project(Path.cwd() / "_")
    result = agent.query(
        question,
        max_hops=max_hops,
        budget=budget,
        project_filter=project,
        include_superseded=include_superseded,
        include_memory=include_memory,
        memory_project=memory_project,
    )
    typer.echo(result)
