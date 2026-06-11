"""`membox list-entities` and `membox list-relations` commands."""

from __future__ import annotations

import typer
from rich.table import Table

from membox.cli._common import console, make_agent


def list_entities(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """List all entities in the knowledge graph."""
    agent = make_agent(db, no_llm=True)
    entities = agent.list_entities()
    table = Table(title="Entities")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Type")
    for entity in entities:
        table.add_row(str(entity.id), entity.name, entity.type)
    console.print(table)


def list_relations(
    db: str = typer.Option("memory.db", "--db", help="Path to SQLite database file"),
) -> None:
    """List all relations in the knowledge graph."""
    agent = make_agent(db, no_llm=True)
    relations = agent.list_relations()
    table = Table(title="Relations")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Source", style="green")
    table.add_column("Predicate", style="yellow")
    table.add_column("Target", style="green")
    table.add_column("Status")
    for rel in relations:
        status = f"superseded by {rel.superseded_by}" if rel.superseded_by is not None else ""
        table.add_row(str(rel.id), rel.source_name, rel.predicate, rel.target_name, status)
    console.print(table)
