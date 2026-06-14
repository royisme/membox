"""Shared Typer option definitions for the membox CLI."""

from __future__ import annotations

import typer

from membox.config import default_db_path

DB_OPTION = typer.Option(
    default_db_path(),
    "--db",
    envvar="MEMBOX_DB",
    help="SQLite database path (env: MEMBOX_DB; default: ~/.membox/membox.db).",
)
