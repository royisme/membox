# Agent Architecture Boundaries

Read this file before changing storage, retrieval, extraction, embedding, normalization, public API, or CLI behavior.

## Target Module Boundaries

The target layout follows `docs/spec.md`:

- `schema.py`: Pydantic models and public data shapes.
- `store.py`: SQLite schema creation, CRUD, deduplication persistence, evidence links, BFS retrieval.
- `extract.py`: LLM extraction via `Protocol`; implementations must be injectable.
- `embed.py`: Embedding via `Protocol`; implementations must be injectable and optional.
- `normalize.py`: Predicate normalization and synonym dictionaries.
- `agent.py`: `MemoryAgent` orchestration layer.
- `cli.py`: Typer CLI entry point.
- `ast_parser.py`: Optional tree-sitter based source analysis.
- `py.typed`: Keep package typed per PEP 561.

When implementing features, keep storage, LLM calls, CLI presentation, and orchestration separate.

## SQLite Safety

- Enable `PRAGMA foreign_keys=ON` for every connection.
- Use WAL mode for concurrent local access.
- Protect find-or-create critical sections with an `RLock` or equivalent synchronization.
- Prefer per-thread connections over one shared global connection.
- Tests must verify foreign keys actually take effect; do not assume SQLite defaults.

## CLI Rules

The CLI is the primary user/agent interface.

Expected commands from the spec:

- `membox ingest "..." --source "..."`
- `membox ingest-file docs/architecture.md --db memory.db`
- `membox query "..." --max-hops 2`
- `membox list-entities --db memory.db`
- `membox list-relations --db memory.db`
- `membox analyze-src src/ --language python --db memory.db` when AST support exists.

Implementation rules:

- Use Typer type annotations so `--help` remains self-describing.
- Use Rich only for presentation; keep business logic out of the CLI layer.
- All commands that touch storage should accept an explicit `--db` option or use a documented default.
- Do not require network access for basic CLI smoke tests.
