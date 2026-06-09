"""membox CLI — command-line interface for coding agents."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="membox",
    help="Local knowledge graph + RAG memory layer for coding agents.",
    no_args_is_help=True,
)


@app.command()  # type: ignore[misc]
def version() -> None:
    """Show membox version."""
    from membox import __version__

    typer.echo(f"membox {__version__}")
