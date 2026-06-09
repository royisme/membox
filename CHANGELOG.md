# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- init: project skeleton with uv, ruff, mypy, pytest, pre-commit, CI (0fbded1)
- chore: stop tracking .crew/ and add to .gitignore (1869ec5)

### Fixed

- fix: suppress typer untyped decorator mypy error (78c3f71)
- fix: re-add type: ignore for typer, allow_unused_ignores in mypy (640902b)

### Documentation

- docs: add code-standards and workflow guides for humans and AI agents (985dbd8)
- docs: add project spec document (ae20c9e)
- docs: add implementation roadmap with phased delivery plan (ca50ba6)
- docs: add CLI (typer), skill-based agent integration, and tree-sitter AST to spec and roadmap (840b4b5)
- docs: restructure roadmap bottom-up — framework → entry → features → extensions → plugins (6bc9300)
