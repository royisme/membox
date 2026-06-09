# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Context Loading

Load the minimal context for the task type — do not read all docs on every session:

| Task | Read first |
|---|---|
| Product direction, data model, architecture, storage, retrieval, CLI behavior | `docs/spec.md` |
| Module placement, SQLite safety, CLI implementation | `docs/agent/02-architecture-boundaries.md` |
| Dependencies, tests, validation, release automation | `docs/workflow.md` |
| Coding style, typing, commit conventions | `docs/code-standards.md` |
| Implementation order / phase planning | `docs/roadmap.md` |
| Filesystem orientation | `docs/repository-map.md` |

If documents conflict, precedence: `docs/spec.md` > `docs/roadmap.md` > `docs/code-standards.md` > `docs/workflow.md` > `pyproject.toml`.

## Commands

```bash
uv sync                              # install all dependencies
uv run pytest                        # run tests
uv run pytest -x                     # stop on first failure
uv run pytest --cov                  # run with coverage report
uv run pytest tests/test_foo.py::test_bar  # run a single test
uv run ruff check --fix .            # lint with auto-fix
uv run ruff format .                 # format
uv run mypy src                      # type check (strict)
uv run pre-commit run --all-files    # run all pre-commit checks

# After creating, moving, or deleting files:
uv run python scripts/update_repository_map.py

# Release automation (do not edit version fields manually):
uv run python scripts/bump_version.py x.y.z
uv run python scripts/generate_changelog.py --version x.y.z
```

## Architecture

Membox is a **local knowledge graph + RAG memory layer** for coding agents. Core constraints:

- No external services, HTTP/MCP servers, background daemons, or hosted vector databases by default.
- Storage is a local SQLite file; multi-process/multi-agent safe via WAL mode + per-thread connections.
- Core logic is Python + SQLite directly — no heavy frameworks that hide critical behaviors.

Target module layout under `src/membox/`:

| Module | Responsibility |
|---|---|
| `schema.py` | Pydantic models and public data shapes |
| `store.py` | SQLite CRUD, deduplication, evidence links, BFS retrieval |
| `extract.py` | LLM extraction via injectable `Protocol` |
| `embed.py` | Embedding via injectable, optional `Protocol` |
| `normalize.py` | Predicate normalization and synonym dictionaries |
| `agent.py` | `MemoryAgent` orchestration layer |
| `cli.py` | Typer entry point — presentation only, no business logic |
| `ast_parser.py` | Optional tree-sitter source analysis |

Keep storage, LLM calls, CLI presentation, and orchestration in separate layers.

## Key Rules

- **Branches**: never commit to `main` or `develop` — always use `feature/*` or `fix/*`.
- **Tests**: no mocked internals; mock only at I/O/network/time boundaries. Use fake extractor/embedder in tests. Coverage ≥ 80% is enforced by CI.
- **SQLite**: enable `PRAGMA foreign_keys=ON`, WAL mode, per-thread connections, and `RLock` for find-or-create.
- **Types**: `from __future__ import annotations` in every file; strict mypy; all public APIs annotated.
- **Docstrings**: Google style, required for all public functions/classes.
- **Every Python file** under `src/`, `scripts/`, and `tests/` must start with a module docstring or leading `#` comment.
- **`docs/repository-map.md` is generated** — never edit manually; run the update script after structural changes.
- Pre-commit hooks run ruff, mypy, and branch protection. If a hook fails, fix and re-commit — never `--no-verify`.
