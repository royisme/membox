"""Synchronize the project version across release metadata files."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_INIT = ROOT / "src" / "membox" / "__init__.py"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$")


def parse_args() -> argparse.Namespace:
    """Parse the target semantic version from the command line."""
    parser = argparse.ArgumentParser(description="Bump membox version metadata.")
    parser.add_argument("version", help="Target version, for example: 0.2.0")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify files already contain the target version without writing changes.",
    )
    return parser.parse_args()


def validate_version(version: str) -> str:
    """Validate the supported release version format."""
    normalized = version.removeprefix("v")
    if not VERSION_PATTERN.fullmatch(normalized):
        message = (
            f"Invalid version {version!r}; expected MAJOR.MINOR.PATCH, optionally prefixed by v."
        )
        raise SystemExit(message)
    return normalized


def replace_once(content: str, pattern: str, replacement: str, path: Path) -> str:
    """Replace exactly one metadata assignment and fail on ambiguous files."""
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count != 1:
        message = f"Expected exactly one version field in {path.relative_to(ROOT)}; found {count}."
        raise SystemExit(message)
    return updated


def planned_updates(version: str) -> dict[Path, str]:
    """Return file contents after applying the target version."""
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    package_init = PACKAGE_INIT.read_text(encoding="utf-8")
    return {
        PYPROJECT: replace_once(
            pyproject,
            r'^version = "[^"]+"$',
            f'version = "{version}"',
            PYPROJECT,
        ),
        PACKAGE_INIT: replace_once(
            package_init,
            r'^__version__ = "[^"]+"$',
            f'__version__ = "{version}"',
            PACKAGE_INIT,
        ),
    }


def main() -> None:
    """Update or verify project version metadata."""
    args = parse_args()
    version = validate_version(args.version)
    updates = planned_updates(version)

    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, content in updates.items()
            if path.read_text(encoding="utf-8") != content
        ]
        if stale:
            formatted = "\n".join(f"- {path}" for path in stale)
            message = f"Version metadata is not set to {version}:\n{formatted}"
            raise SystemExit(message)
        return

    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")

    sys.stdout.write(f"Bumped membox version to {version}\n")


if __name__ == "__main__":
    main()
