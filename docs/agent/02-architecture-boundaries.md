# Agent Architecture Boundaries

Read this file before changing storage, retrieval, extraction, embedding, normalization, public API, or CLI behavior.

## Target Module Boundaries

The target layout follows `docs/spec.md`:

- `model/schema.py`: Pydantic models and public data shapes.
- `config.py`: `MemboxConfig` — provider, model, base_url, and API-key selection per capability (extraction / embedding).
- `core/store/`: SQLite storage package behind the `KnowledgeStore` facade:
  - `connection.py`: per-thread connections, WAL/PRAGMAs, transaction context manager, `RLock`.
  - `migrations.py`: `PRAGMA user_version` migration machinery (ordered `MIGRATIONS` list).
  - `entities.py`: entity CRUD, find-or-create deduplication, aliases.
  - `relations.py`: relation CRUD and evidence links.
  - `documents.py`: document persistence.
  - `retrieval.py`: BFS multi-hop retrieval.
- `services/extraction.py`: LLM extraction via the domain-level `LLMExtractor` `Protocol`; implementations must be injectable and delegate HTTP to `providers/`.
- `services/embedding.py`: Embedding via the domain-level `Embedder` `Protocol`; implementations must be injectable and optional.
- `services/prompts/`: Prompt templates as module-level constants/builders — no inline prompts in service logic.
- `services/ast_parser.py`: Optional tree-sitter based source analysis.
- `providers/base.py`: Low-level `ChatClient` / `EmbedClient` `Protocol`s — auth, request shape, and error normalization only; no domain logic, no prompts.
- `providers/openai_compat.py`: OpenAI-compatible adapter; covers OpenAI/Ollama/vLLM/DeepSeek via `base_url`.
- `core/normalize.py`: Predicate normalization and synonym dictionaries.
- `core/agent.py`: `MemoryAgent` orchestration layer.
- `cli/`: Typer CLI package; `cli/__init__.py` assembles and exposes `app` (the `membox` entry point), `cli/commands/` holds one module per command group.
- `py.typed`: Keep package typed per PEP 561.

When implementing features, keep storage, LLM calls, CLI presentation, and orchestration separate. Services never speak HTTP directly; providers never contain business logic.

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
