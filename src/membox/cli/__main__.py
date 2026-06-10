"""Allow ``python -m membox.cli`` — used by the M6 worker spawn path."""

from __future__ import annotations

from membox.cli import app

if __name__ == "__main__":
    app()
