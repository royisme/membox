# membox

A local knowledge graph + RAG memory layer for coding agents.

**Status: research / experimental.** This repository is the working space where
I explore how a small, self-contained memory system should be designed and
implemented for agents that need persistent, queryable knowledge without
relying on hosted databases or external services. Treat it as a reference
implementation, not a production dependency.

## What it is

- A local-first store: everything lives in a single SQLite file on disk.
- A typed knowledge graph: entities, relations, and source documents, with
  schema-enforced shapes (Pydantic).
- An injectable LLM layer: extraction and embedding go through `Protocol`s,
  so the same code paths exercise with `DummyExtractor` / `DummyEmbedder`
  in tests and with real providers (`OpenAI`, `Ollama`, `vLLM`, `DeepSeek` —
  anything OpenAI-compatible via `base_url`) in production.
- A CLI designed for agent invocation: `ingest` / `query` / `list-entities` /
  `list-relations` are intentionally shaped so that an agent can call them
  from a skill file without an HTTP daemon or MCP server.

## What it is not

- Not a hosted service. No background daemons, no network calls except to
  the configured LLM provider.
- Not a vector database. Embeddings are optional; if absent, deduplication
  falls back to exact / casing-normalized matching.
- Not a framework. The design favors direct, readable code over abstraction.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Optional LLM dependencies (OpenAI client, tree-sitter) install with:

```bash
uv sync --extra llm
```

## Quick start

```bash
# Run tests
uv run pytest

# Lint & format
uv run ruff check .
uv run ruff format .

# Type check (strict)
uv run mypy src
```

## CLI usage

```bash
membox ingest "Alice works at Acme." --source memo.txt
membox ingest-file README.md
membox query "Where does Alice work?" --max-hops 2
membox list-entities
membox list-relations
```

By default `membox` reads `OPENAI_API_KEY` from the environment for
extraction and embedding. Pass `--no-llm` to use the deterministic dummy
backends (useful for tests and offline exploration).

## Python API

```python
from membox import MemoryAgent, OpenAIExtractor, OpenAIEmbedder

agent = MemoryAgent(
    store="memory.db",
    extractor=OpenAIExtractor(),
    embedder=OpenAIEmbedder(),
)
agent.ingest("Alice works at Acme.", source="memo.txt")
result = agent.query("Where does Alice work?", max_hops=2)
```

Without an LLM:

```python
from membox import MemoryAgent, DummyExtractor, DummyEmbedder

agent = MemoryAgent("memory.db", extractor=DummyExtractor(), embedder=DummyEmbedder())
```

## Layout

```
src/membox/
├── model/       Pydantic data shapes
├── core/        SQLite storage (store/), predicate normalization, agent
├── services/    Domain layer: extraction, embedding, prompt templates
├── providers/   Protocol adapters (OpenAI-compatible HTTP, future Gemini)
└── cli/         Typer commands — presentation only
```

See [docs/spec.md](docs/spec.md) for the full design and [docs/roadmap.md](docs/roadmap.md)
for current progress (Phases 1–7 verified via tests; 8–10 pending).

## Why these design choices

| Decision | Rationale |
|---|---|
| SQLite + WAL + per-thread connection | Zero ops overhead; multi-process / multi-agent safe without a database server |
| Direct SQL (no ORM) | The find-or-create critical section and per-thread lifecycle need fine-grained control that ORM abstractions hide |
| Pydantic for data shapes | One schema definition powers validation, serialization, and the public API |
| `Protocol`-injected extractors / embedders | Tests run without live APIs; production swaps in OpenAI/Ollama/vLLM without touching the agent |
| Schema migrations via `PRAGMA user_version` | Forward-compatible schema changes for existing `.db` files, no Alembic dependency |
| Skill file as agent integration surface | Agent reads the skill and calls the CLI directly — no MCP / HTTP daemon required |

## License

MIT
