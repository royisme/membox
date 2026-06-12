"""Generate the repository structure map for agents and reviewers.

Functional Python files under src, scripts, and tests must start with a module docstring or
leading comment so tools can reuse the file purpose without asking an LLM to inspect it.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "repository-map.md"

EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "python_install__",
    "venv",
}
EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo", ".so"}
EXCLUDED_FILES = {".coverage", "coverage.xml", "uv.lock"}
EXCLUDED_RELATIVE_PATHS = {Path("docs/reference")}
FUNCTIONAL_ROOTS = {"src", "scripts", "tests"}
MAX_DEPTH = 4


def get_git_ignored_paths() -> set[Path]:
    """Return relative paths ignored by git via git ls-files."""
    try:
        res = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        paths = set()
        for line in res.stdout.splitlines():
            line = line.strip()
            if line:
                paths.add(Path(line.rstrip("/")))
        return paths
    except Exception:
        return set()


GIT_IGNORED_PATHS = get_git_ignored_paths()


def should_skip(path: Path) -> bool:
    """Return whether a path is generated noise rather than repository structure."""
    relative = path.relative_to(ROOT)
    if relative in EXCLUDED_RELATIVE_PATHS:
        return True
    if any(relative == p or p in relative.parents for p in GIT_IGNORED_PATHS):
        return True
    if path.name in EXCLUDED_DIRS or path.name in EXCLUDED_FILES:
        return True
    return path.is_file() and path.suffix in EXCLUDED_FILE_SUFFIXES


def sort_key(path: Path) -> tuple[int, str]:
    """Sort directories before files, then by lowercase name."""
    return (0 if path.is_dir() else 1, path.name.lower())


def is_functional_python_file(path: Path) -> bool:
    """Return whether a file needs a reusable purpose header."""
    if path.suffix != ".py" or not path.is_file():
        return False
    try:
        top_level = path.relative_to(ROOT).parts[0]
    except ValueError:
        return False
    return top_level in FUNCTIONAL_ROOTS


def first_sentence(text: str) -> str:
    """Return a compact single-line description for the repository map."""
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    sentence, separator, _rest = normalized.partition(". ")
    return f"{sentence}." if separator else sentence


def python_header(path: Path) -> str:
    """Extract a Python module docstring or leading comment from a file."""
    source = path.read_text(encoding="utf-8")
    try:
        module = ast.parse(source)
    except SyntaxError:
        module = None

    if module is not None:
        docstring = ast.get_docstring(module)
        if docstring:
            return first_sentence(docstring)

    comments: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            if comments:
                break
            continue
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            continue
        break
    return first_sentence(" ".join(comments))


def file_description(path: Path) -> str:
    """Return a reusable file purpose description for supported file types."""
    if path.suffix == ".py":
        return python_header(path)
    return ""


def validate_functional_headers(paths: list[Path]) -> None:
    """Fail when functional files do not expose a reusable top-level purpose."""
    missing = [
        path.relative_to(ROOT)
        for path in paths
        if is_functional_python_file(path) and not file_description(path)
    ]
    if missing:
        formatted = "\n".join(f"- {path}" for path in missing)
        message = (
            "Functional Python files must start with a module docstring or leading comment "
            "so repository tools can reuse their purpose. Missing headers:\n"
            f"{formatted}"
        )
        raise SystemExit(message)


def render_tree(directory: Path, depth: int = 0) -> list[str]:
    if depth > MAX_DEPTH:
        return []

    lines: list[str] = []
    children = sorted(
        (child for child in directory.iterdir() if not should_skip(child)),
        key=sort_key,
    )

    for child in children:
        relative = child.relative_to(ROOT)
        indent = "  " * depth
        marker = "/" if child.is_dir() else ""
        description = file_description(child) if child.is_file() else ""
        suffix = f" — {description}" if description else ""
        lines.append(f"{indent}- `{relative}{marker}`{suffix}")
        if child.is_dir() and depth < MAX_DEPTH:
            lines.extend(render_tree(child, depth + 1))

    return lines


def iter_repository_files(directory: Path) -> list[Path]:
    """Return all non-excluded files under the repository root."""
    files: list[Path] = []
    for child in directory.rglob("*"):
        relative = child.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if any(
            relative == excluded or excluded in relative.parents
            for excluded in EXCLUDED_RELATIVE_PATHS
        ):
            continue
        if should_skip(child):
            continue
        if child.is_file():
            files.append(child)
    return files


def main() -> None:
    validate_functional_headers(iter_repository_files(ROOT))
    tree = "\n".join(render_tree(ROOT))
    content = f"""# Repository Map

> Generated by `scripts/update_repository_map.py`. Do not edit manually.
> Run `uv run python scripts/update_repository_map.py` after creating, moving, or deleting files.

This file gives agents a lightweight, deterministic repository outline without spending LLM context on filesystem discovery.
Functional Python files include their module header so tools can reuse the file purpose directly.

## Directory Outline

{tree}
"""
    OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
