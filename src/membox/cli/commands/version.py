"""`membox version` command."""

from __future__ import annotations

import typer


def version() -> None:
    """Show membox version."""
    from membox import __version__

    typer.echo(f"membox {__version__}")
