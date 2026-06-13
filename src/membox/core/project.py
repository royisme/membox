"""Project-scope inference helpers shared by core and CLI code."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def infer_project(path: Path) -> str:
    """Infer the project name for a file by walking up to the nearest git root.

    Walks upward from *path*'s directory looking for a ``.git`` entry (which
    may be a directory in a normal clone or a file in a git worktree). If a git
    root is found its directory name is returned; otherwise the immediate
    parent directory name is used as a fallback.
    """
    current = path.parent
    while True:
        if (current / ".git").exists():
            return current.name
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path.parent.name
