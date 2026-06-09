# Membox — Project Specification

> **Version**: 0.1.0 · **Status**: Draft · **License**: MIT

## 1. Project Positioning

Membox is a **local Knowledge Graph + RAG Memory Layer** designed to provide unified memory services for **coding agents** (such as Cursor, Copilot, Cline, Aider, etc.).

Core Propositions:

- **Hands-on Implementation** — No reliance on external services like Neo4j, Weaviate, or Pinecone. All logic is written from scratch in Python + SQLite, allowing developers to fully understand and control every line of code.
- **CLI-First** — Delivered as a command-line tool. Coding agents learn to interact via shell commands through a **skill file** (instruction document), eliminating the need for MCP or HTTP servers.
- **Zero External Services** — File-level SQLite storage requiring no database server process, ideal for single-machine local development environments.
- **Agent Sharing** — Multiple coding agents share memory through the same SQLite database file, preventing fragmented context.

## 2. Target Users & Scenarios

| Role | Scenario |
|------|----------|
| Coding Agent (Cursor/Copilot/Cline/Aider…) | Queries project architecture, historical decisions, and API usage during coding sessions. |
| Developer | Searches memory after ingesting documents via the agent, or queries directly using the Python API. |
| CI/CD Pipeline | Automatically extracts knowledge from commit messages and PR descriptions to ingest into the database. |

## 3. Core Features

### 3.1 Knowledge Graph Storage

Centered around the **Entity-Relation-Entity** triple data model:

```
(Entity) --[predicate]--> (Entity)
```

- **Entity**: Projects, technologies, modules, concepts, names, etc., supporting aliases.
- **Relation**: Directed edges with predicates, such as `uses`, `develops`, `depends_on`.
- **Evidence Provenance**: Each relation can point to multiple document snippets as source evidence.

### 3.2 Document Ingestion & Knowledge Extraction

Ingests natural language documents and uses an LLM to automatically extract entity and relation triples:

```
Document → LLM Extraction → Triples → Ingestion (Deduplication + Disambiguation)
```

### 3.3 Multi-hop Retrieval

Expands outwards from seed entities using BFS along relation edges, with a configurable `max_hops`:

```
seed → 1-hop neighbors → 2-hop neighbors → ... → max_hops
```

Retrieval results contain the complete paths and original source text (evidence), assembled into a structured prompt.

### 3.4 Entity Disambiguation

A three-tier cascading strategy to prevent the same concept from being created as duplicate entities:

1. **Exact Alias Matching** — Merges directly if an alias matches in the alias table.
2. **Embedding Similarity** — Considers entities of the same type as identical if cosine similarity is ≥ 0.85.
3. **Creation** — Creates a new entity if the first two checks miss.

### 3.5 Predicate Normalization

Normalizes semantically equivalent predicates into standard forms:

- `developed` / `develop` / `开发` → `develops`
- Lowercase normalization + English/Chinese synonym dictionary.

## 4. Architectural Design

### 4.1 Tech Stack

| Dimension | Selection | Rationale |
|------|------|------|
| Language | Python 3.13 | The standard language in the coding agent ecosystem. |
| Storage | SQLite (WAL Mode) | Zero operations overhead, file-based storage, cross-process safe. |
| CLI | **typer** + rich | Type annotations define CLI commands automatically with built-in help and shell completion; rich formatting improves readability. |
| Validation | Pydantic | Data model validation and serialization. |
| LLM Interface | Protocol Class | Allows injection of any LLM implementation; testing does not depend on live APIs. |
| Embedding | Protocol Class | Allows injection of any embedding implementation; falls back to string deduplication when unavailable. |
| Code Analysis (Optional) | **tree-sitter** | Multi-language AST parsing to extract structural code knowledge (signatures, class structure, import dependencies). |
| Agent Integration | **Skill File** | Non-MCP / Non-HTTP; agents read the skill instructions and invoke the `membox` CLI directly. |

### 4.2 Data Model

```sql
-- Entities
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'thing',
    embedding   BLOB,                       -- float32 vector
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Entity Aliases
CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    alias       TEXT    NOT NULL,
    PRIMARY KEY (entity_id, alias)
);

-- Relations (Triples)
CREATE TABLE relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES entities(id),
    target_id   INTEGER NOT NULL REFERENCES entities(id),
    predicate   TEXT    NOT NULL,
    UNIQUE(source_id, target_id, predicate)  -- Triple deduplication
);

-- Documents (Raw Text)
CREATE TABLE documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    source      TEXT,                        -- Source identifier (file path, URL, etc.)
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Relation-Document Evidence (Many-to-Many)
CREATE TABLE relation_evidence (
    relation_id  INTEGER NOT NULL REFERENCES relations(id),
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    PRIMARY KEY (relation_id, document_id)
);
```

### 4.3 Core Modules

```
src/membox/
├── __init__.py          # Package entry point, exposes public APIs
├── schema.py            # Pydantic model definitions
├── store.py             # SQLite storage layer (schema, CRUD, BFS retrieval)
├── extract.py           # LLM knowledge extraction (Protocol + OpenAI implementation)
├── embed.py             # Embedding computation (Protocol + OpenAI implementation)
├── normalize.py         # Predicate normalization and synonym dictionary
├── agent.py             # MemoryAgent — Orchestration layer
├── cli.py               # Typer CLI entry point (ingest / query / list commands)
├── ast_parser.py        # Tree-sitter code analysis (optional module)
└── py.typed             # PEP 561 marker
```

### 4.4 Key Design Decisions

| Decision | Selection | Rationale |
|------|------|------|
| Concurrency Safety | Per-thread connection + WAL + `RLock` | SQLite WAL allows concurrent reads/writes; `RLock` protects the find-or-create critical section. |
| Foreign Keys | `PRAGMA foreign_keys=ON` | Disabled by default in SQLite; must be explicitly enabled. |
| Triple Uniqueness | `UNIQUE(source_id, target_id, predicate)` | Only allows a single edge for the same predicate between the same two entities. |
| LLM/Embedding Decoupling | Protocol | Mock implementations can be injected so tests run independently of external APIs. |
| No-Embedding Fallback | Exact match + casing normalization string deduplication | Ensures core features work when an OpenAI key is absent. |
| Agent Integration Path | Skill file (CLI instruction doc) | Agent reads the skill and runs shell commands; no MCP / HTTP daemon required. |
| CLI Framework | typer + rich | Type annotations act as interface definitions; agents learn by inspecting `--help`. |
| Codebase Analysis | tree-sitter (optional) | Multi-language AST parsing to extract structural knowledge like module dependencies and call graphs. |

## 5. Interface Design

### 5.1 CLI Commands (Primary interface for coding agents)

Agents learn to use the following commands via the skill file without needing to understand the Python API:

```bash
# Ingest text
membox ingest "codebase-rag is implemented in Python" --source "README.md"

# Ingest file
membox ingest-file docs/architecture.md --db memory.db

# Query memory
membox query "What technologies are used in the project?" --max-hops 2

# List entities
membox list-entities --db memory.db

# List relations
membox list-relations --db memory.db

# Analyze source structure (tree-sitter, optional)
membox analyze-src src/ --language python --db memory.db
```

All commands support `--help`, which allows agents to discover usage details automatically.

### 5.2 Python API (Advanced Usage)

```python
from membox import MemoryAgent, OpenAIExtractor, OpenAIEmbedder

agent = MemoryAgent(
    extractor=OpenAIExtractor(client),   # Required
    embedder=OpenAIEmbedder(client),     # Optional; falls back to string-based deduplication if omitted
    db_path="memory.db",                 # SQLite file path
)
```

### 5.3 Core Methods

```python
# Ingest document → automatically extracts triples and writes to database
agent.ingest(text: str, source: str | None = None) -> None

# Query → performs BFS starting from seed entities, returning structured prompt context
agent.query(question: str, max_hops: int = 2) -> str

# List all entities in the graph
agent.list_entities() -> list[Entity]

# List all relations in the graph
agent.list_relations() -> list[Relation]
```

## 6. Quality Requirements

### 6.1 Test Coverage

Tests do not depend on external APIs (LLMs and Embeddings are mocked) and cover the following scenarios:

- **Entity Disambiguation** — Exact string deduplication / casing deduplication / embedding synonym deduplication / negative scenarios (unrelated entities are not merged).
- **Relation Deduplication** — `UNIQUE` constraint + many-to-many evidence association.
- **Predicate Normalization** — developed / develop / 开发 → develops.
- **Multi-hop Retrieval** — 2-hop recall validation / 3-hop recall validation / unrelated entities are not recalled.
- **Context Aggregation** — Complete path reconstruction for multi-hop paths.
- **Provenance** — Trace relations back to source texts.
- **Concurrency Safety** — Multi-threaded writes run without errors, count accurately, and ensure identical concurrent entities resolve to a single record.
- **Foreign Key Constraints** — Verify schema enforcement is active.

### 6.2 Code Quality

| Tool | Purpose | Configuration |
|------|------|------|
| Ruff | lint + format | target py313, line-length 100 |
| mypy | Type Checking | strict mode |
| pytest | Testing | importlib mode, strict markers |
| pre-commit | Git hooks | ruff, trailing whitespace, large files, merge conflicts |
| CI (GitHub Actions) | Continuous Integration | Automatic linting + type checking + testing |

### 6.3 Coverage Target

Minimum 80% (`fail_under = 80`), with `show_missing = true`.

## 7. Dependencies

### Runtime

- `pydantic` — Data model validation
- `typer` — CLI framework (type annotations map to command definitions)
- `rich` — Terminal output formatting

### Optional

- `openai` — OpenAI API client (needed for live demo and real LLM extraction)
- `tree-sitter` — Multi-language AST parsing (needed for codebase structure analysis)

### Development

- `pytest >= 8` / `pytest-cov >= 6` — Testing
- `ruff >= 0.11` — lint + format
- `mypy >= 1.15` — Type Checking
- `pre-commit >= 4` — Git hooks

## 8. Extension Roadmap

### Planned (see roadmap.md)

| Target | Description | Phase |
|------|------|------|
| Skill File | Skill instruction documents to teach agents how to use the CLI. | Phase 9 |
| Codebase Analysis | Multi-language AST parsing using tree-sitter to extract module dependencies, call graphs, and class structures. | Phase 10 |

### Future Options

| Direction | Description | Trigger Condition |
|------|------|----------|
| Vector Index Upgrade | Replace `find_similar_entity` with sqlite-vss, FAISS, or Lance. | Entity count > ~100k |
| Automatic Predicate Clustering | Automatic synonym predicate discovery via embedding clustering. | Predicate type explosion |
| Confidence & Auditing | Add `confidence` / `merged_from` fields to entities. | When human-in-the-loop review is required |
| Hybrid Retrieval | BM25 over `documents.content` + Vector search. | Pure graph recall becomes insufficient |
| Temporal Decay | Add `confidence` / `extracted_at` to `relation_evidence`. | Time-sensitive knowledge requirements |
