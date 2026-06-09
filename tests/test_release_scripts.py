"""Tests for release automation helper scripts."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a repository script module by file path under pytest importlib mode."""
    spec = spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bump_version = load_script("bump_version")
generate_changelog = load_script("generate_changelog")


def test_validate_version_accepts_plain_and_prefixed_versions():
    assert bump_version.validate_version("0.2.0") == "0.2.0"
    assert bump_version.validate_version("v1.2.3") == "1.2.3"


def test_validate_version_rejects_invalid_versions():
    with pytest.raises(SystemExit):
        bump_version.validate_version("next")


def test_replace_once_updates_exactly_one_assignment():
    content = 'version = "0.1.0"\nname = "membox"\n'

    updated = bump_version.replace_once(
        content, r'^version = "[^"]+"$', 'version = "0.2.0"', Path("pyproject.toml")
    )

    assert updated == 'version = "0.2.0"\nname = "membox"\n'


def test_render_section_groups_conventional_commits():
    section = generate_changelog.render_section(
        None,
        [
            generate_changelog.Commit(hash="abcdef123456", subject="feat(cli): add query command"),
            generate_changelog.Commit(
                hash="123456abcdef", subject="fix(store): enforce foreign keys"
            ),
            generate_changelog.Commit(hash="999999999999", subject="docs: update workflow"),
        ],
    )

    assert "## [Unreleased]" in section
    assert "### Added" in section
    assert "- feat(cli): add query command (abcdef1)" in section
    assert "### Fixed" in section
    assert "- fix(store): enforce foreign keys (123456a)" in section
    assert "### Documentation" in section


def test_update_changelog_replaces_target_section():
    existing = "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n- old entry\n"
    generated = "## [Unreleased]\n\n### Changed\n\n- new entry\n"

    updated = generate_changelog.update_changelog(existing, generated, None)

    assert "- new entry" in updated
    assert "- old entry" not in updated
