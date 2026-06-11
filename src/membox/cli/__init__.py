"""membox CLI — Typer app assembly.

Exposes ``app`` so the ``membox = "membox.cli:app"`` entry point keeps
working. Command implementations live in :mod:`membox.cli.commands`,
presentation only — no business logic.
"""

from __future__ import annotations

import typer

from membox.cli.commands.history import history_app
from membox.cli.commands.ingest import ingest, ingest_file
from membox.cli.commands.listing import list_entities, list_relations
from membox.cli.commands.query import query
from membox.cli.commands.queue import process, queue_status
from membox.cli.commands.version import version

app = typer.Typer(
    name="membox",
    help="Local knowledge graph + RAG memory layer for coding agents.",
    no_args_is_help=True,
)

app.command()(version)
app.command()(ingest)
app.command("ingest-file")(ingest_file)
app.command()(query)
app.command()(process)
app.command("queue")(queue_status)
app.command("list-entities")(list_entities)
app.command("list-relations")(list_relations)
app.add_typer(history_app)

__all__ = ["app"]
