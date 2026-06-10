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
| `model/schema.py` | Pydantic models and public data shapes |
| `config.py` | `MemboxConfig` — provider/model/base_url/API-key per capability |
| `core/store/` | SQLite storage package: `KnowledgeStore` facade over `connection.py`, `migrations.py` (`PRAGMA user_version`), `entities.py`, `relations.py`, `documents.py`, `retrieval.py` |
| `core/normalize.py` | Predicate normalization and synonym dictionaries |
| `core/agent.py` | `MemoryAgent` orchestration layer |
| `services/extraction.py` | LLM extraction via injectable `LLMExtractor` `Protocol` (domain layer, no HTTP) |
| `services/embedding.py` | Embedding via injectable, optional `Embedder` `Protocol` |
| `services/prompts/` | Prompt templates as module-level constants/builders |
| `services/ast_parser.py` | Optional tree-sitter source analysis |
| `providers/base.py` | Low-level `ChatClient` / `EmbedClient` `Protocol`s (auth/request/error only) |
| `providers/openai_compat.py` | OpenAI-compatible adapter (OpenAI/Ollama/vLLM/DeepSeek via `base_url`) |
| `cli/` | Typer package — `cli/__init__.py` exposes `app`; `cli/commands/` one module per command group; presentation only, no business logic |

Keep storage, LLM calls, CLI presentation, and orchestration in separate layers. Services never speak HTTP directly; providers contain no business logic.

## Key Rules

- **Branches**: never commit to `main` or `develop` — always use `feature/*` or `fix/*`.
- **Tests**: no mocked internals; mock only at I/O/network/time boundaries. Use fake extractor/embedder in tests. Coverage ≥ 80% is enforced by CI.
- **SQLite**: enable `PRAGMA foreign_keys=ON`, WAL mode, per-thread connections, and `RLock` for find-or-create.
- **Types**: `from __future__ import annotations` in every file; strict mypy; all public APIs annotated.
- **Docstrings**: Google style, required for all public functions/classes.
- **Every Python file** under `src/`, `scripts/`, and `tests/` must start with a module docstring or leading `#` comment.
- **`docs/repository-map.md` is generated** — never edit manually; run the update script after structural changes.
- Pre-commit hooks run ruff, mypy, and branch protection. If a hook fails, fix and re-commit — never `--no-verify`.

## Working Style

**Orchestrator model: the main model presides, subagents execute.** The main (strong) model acts as the resident advisor and dispatcher — it holds the design context, makes judgment calls, decomposes work, and reviews results. It should NOT burn its own context window on bulk reading, repetitive comprehension, or mechanical edits: delegate those to subagents (Agent tool) and consume only their conclusions. This keeps the main context lean (raw file dumps and grep output stay in the subagent's context, not the orchestrator's) and cuts cost (bulk tokens are processed at cheaper-model prices).

**Dispatch rules:**

- Delegate any well-scoped, multi-step work: mechanical refactors, broad searches, repetitive file comprehension ("read these N files and summarize X"), doc syncs, test-fix loops.
- Run subagents in the background when design discussion should continue in parallel.
- Every dispatch must include: precise scope, what is already done (do not redo), hard constraints (project rules above), and a verification gate (pytest + ruff + mypy must be green).
- Subagents return distilled conclusions, not raw content — instruct them to report findings/diffs/verdicts, never to paste whole files back.

**Match model to task difficulty** via the Agent tool's `model` parameter:

| Model | Use for |
|---|---|
| `haiku` | Bulk + mechanical: renames, import-path updates, grep sweeps, file moves, reading many files to extract/summarize specific facts, regenerating `repository-map.md`, formatting fixes |
| `sonnet` | Routine implementation: well-specified refactors, writing tests from a clear spec, doc updates, fixing lint/type errors |
| (default/inherit — orchestrator only) | Design decisions, architecture changes, ambiguous debugging, spec judgment, reviewing subagent output. Reserve the strong model for thinking, not for reading. |

Escalation path (advisor pattern, inverted): when a cheap-model subagent reports it hit a judgment call beyond its brief — ambiguous spec, conflicting constraints, surprising findings — it must stop and return the question instead of guessing; the orchestrator decides and re-dispatches. When in doubt about difficulty upfront, prefer the stronger model; a failed cheap run costs more than it saves.
