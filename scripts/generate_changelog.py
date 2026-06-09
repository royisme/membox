"""Generate CHANGELOG.md sections from conventional commit history."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
HEADER = """# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
"""
CONVENTIONAL_COMMIT = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<description>.+)$"
)
SECTION_ORDER = [
    "Breaking Changes",
    "Added",
    "Changed",
    "Fixed",
    "Dependencies",
    "Documentation",
    "Tests",
]
TYPE_TO_SECTION = {
    "feat": "Added",
    "fix": "Fixed",
    "deps": "Dependencies",
    "docs": "Documentation",
    "test": "Tests",
    "tests": "Tests",
    "build": "Changed",
    "chore": "Changed",
    "ci": "Changed",
    "perf": "Changed",
    "refactor": "Changed",
    "style": "Changed",
}


@dataclass(frozen=True)
class Commit:
    """A single git commit relevant to changelog generation."""

    hash: str
    subject: str

    @property
    def short_hash(self) -> str:
        """Return the short commit hash used as a changelog reference."""
        return self.hash[:7]


@dataclass(frozen=True)
class ChangelogEntry:
    """A categorized changelog entry derived from one commit subject."""

    section: str
    text: str


def parse_args() -> argparse.Namespace:
    """Parse changelog generation options."""
    parser = argparse.ArgumentParser(description="Generate CHANGELOG.md from git commits.")
    parser.add_argument(
        "--version",
        help="Release version for the generated section. Defaults to Unreleased.",
    )
    parser.add_argument(
        "--since",
        help="Start revision or tag, exclusive. Defaults to the latest reachable tag.",
    )
    parser.add_argument("--to", default="HEAD", help="End revision, inclusive. Defaults to HEAD.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify CHANGELOG.md is already up to date without writing changes.",
    )
    return parser.parse_args()


def git_executable() -> str:
    """Return the absolute git executable path for subprocess safety checks."""
    executable = shutil.which("git")
    if executable is None:
        message = "git executable not found in PATH."
        raise SystemExit(message)
    return executable


def run_git(args: list[str], *, allow_failure: bool = False) -> str:
    """Run a git command in the repository and return stdout."""
    process = subprocess.run(  # noqa: S603 - arguments are passed without a shell.
        [git_executable(), *args],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        if allow_failure:
            return ""
        command = " ".join(["git", *args])
        message = f"{command} failed:\n{process.stderr.strip()}"
        raise SystemExit(message)
    return process.stdout.strip()


def latest_tag() -> str | None:
    """Return the latest reachable git tag, if one exists."""
    tag = run_git(["describe", "--tags", "--abbrev=0"], allow_failure=True)
    return tag or None


def revision_range(since: str | None, to_revision: str) -> str:
    """Build the git revision range used for changelog collection."""
    start = since or latest_tag()
    if start is None:
        return to_revision
    return f"{start}..{to_revision}"


def collect_commits(since: str | None, to_revision: str) -> list[Commit]:
    """Collect commits in chronological order for deterministic output."""
    output = run_git(
        ["log", "--reverse", "--pretty=format:%H%x1f%s%x1e", revision_range(since, to_revision)],
        allow_failure=True,
    )
    commits: list[Commit] = []
    for raw_record in output.split("\x1e"):
        record = raw_record.strip()
        if not record:
            continue
        commit_hash, separator, subject = record.partition("\x1f")
        if not separator:
            continue
        commits.append(Commit(hash=commit_hash, subject=subject.strip()))
    return commits


def normalize_version(version: str | None) -> str | None:
    """Normalize a version value for section titles."""
    return version.removeprefix("v") if version else None


def section_title(version: str | None) -> str:
    """Return the markdown title for the target changelog section."""
    normalized = normalize_version(version)
    if normalized is None:
        return "## [Unreleased]"
    today = datetime.now(UTC).date().isoformat()
    return f"## [{normalized}] - {today}"


def entry_from_commit(commit: Commit) -> ChangelogEntry:
    """Convert a git commit subject into a categorized changelog entry."""
    match = CONVENTIONAL_COMMIT.fullmatch(commit.subject)
    if match is None:
        text = f"chore: {commit.subject} ({commit.short_hash})"
        return ChangelogEntry(section="Changed", text=text)

    commit_type = match.group("type")
    section = TYPE_TO_SECTION.get(commit_type, "Changed")
    if match.group("breaking"):
        section = "Breaking Changes"
    text = f"{commit.subject} ({commit.short_hash})"
    return ChangelogEntry(section=section, text=text)


def render_section(version: str | None, commits: list[Commit]) -> str:
    """Render one changelog section from categorized commits."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for commit in commits:
        entry = entry_from_commit(commit)
        grouped[entry.section].append(entry.text)

    lines = [section_title(version), ""]
    if not grouped:
        lines.extend(["### Changed", "", "- No changes.", ""])
        return "\n".join(lines)

    for section in SECTION_ORDER:
        entries = grouped.get(section)
        if not entries:
            continue
        lines.extend([f"### {section}", ""])
        lines.extend(f"- {entry}" for entry in entries)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def split_header_and_sections(content: str) -> tuple[str, str]:
    """Split changelog header from version sections."""
    marker = re.search(r"^## ", content, flags=re.MULTILINE)
    if marker is None:
        return content.rstrip() + "\n", ""
    return content[: marker.start()].rstrip() + "\n", content[marker.start() :].lstrip()


def target_section_pattern(version: str | None) -> re.Pattern[str]:
    """Return a regex matching the target section title and body."""
    normalized = normalize_version(version)
    title = r"Unreleased" if normalized is None else re.escape(normalized)
    return re.compile(
        rf"^## \[{title}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE
    )


def update_changelog(existing: str, generated_section: str, version: str | None) -> str:
    """Insert or replace the generated changelog section."""
    header, sections = split_header_and_sections(existing or HEADER)
    pattern = target_section_pattern(version)
    if pattern.search(sections):
        updated_sections = pattern.sub(generated_section.rstrip() + "\n\n", sections, count=1)
    else:
        updated_sections = generated_section.rstrip() + "\n\n" + sections
    return header.rstrip() + "\n\n" + updated_sections.rstrip() + "\n"


def main() -> None:
    """Generate or verify CHANGELOG.md."""
    args = parse_args()
    commits = collect_commits(args.since, args.to)
    generated_section = render_section(args.version, commits)
    existing = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    updated = update_changelog(existing, generated_section, args.version)

    if args.check:
        if existing != updated:
            message = "CHANGELOG.md is not up to date. Run scripts/generate_changelog.py."
            raise SystemExit(message)
        return

    CHANGELOG.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
