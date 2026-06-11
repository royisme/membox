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
2. **Embedding Similarity** — Considers entities of the same type as identical if cosine similarity ≥ `disambiguation_threshold` (configurable; default 0.85 for OpenAI embeddings, 0.70 preset for `embeddinggemma` via Ollama).
3. **Creation** — Creates a new entity if the first two checks miss.

### 3.5 Predicate Normalization

Normalizes semantically equivalent predicates into standard forms:

- `developed` / `develop` / `开发` → `develops`
- Lowercase normalization + English/Chinese synonym dictionary.

### 3.6 Hybrid Retrieval

When graph-only traversal is insufficient (e.g., long natural-language sentences, markdown prose where entities are implicit), membox fuses two retrieval signals:

- **Graph traversal** — BFS along relation edges (existing `max_hops` mechanism).
- **FTS5 BM25 keyword search** — Full-text search over `documents.content` using SQLite's built-in FTS5 engine.

Results from both signals are assembled into one compact context. The `--project` filter scopes retrieval to a single project; omitting it queries the global database.

**Default fusion mode**: `retrieval.fusion_mode="merge"` performs budget-partitioned graph + FTS fusion. The graph pool is produced by `scored_query(...)`; the FTS pool is produced by direct FTS5 BM25 over `documents.content`, capped by `retrieval.fts_fallback_k` (default `10`, `0` disables the FTS chunk channel). The two pools keep separate ranking semantics and are never compared on a shared score scale. Fusion happens during token budgeting with `retrieval.chunk_share` (default `0.4`): triples get the initial graph allowance, chunks get the reserved chunk allowance plus graph leftovers, then unused chunk budget flows back to additional triple lines.

**Fallback compatibility mode**: `retrieval.fusion_mode="fallback"` preserves the old either/or behavior for A/B testing and rollback. When seed-entity resolution finds no graph entities, or BFS yields no candidate relations, retrieval falls back to a direct FTS5 BM25 search over `documents.content` instead of returning an empty result. The question is tokenised into an OR-of-tokens MATCH expression (a phrase match almost never fires for a full natural-language question); the top `retrieval.fts_fallback_k` chunks (default 10, `0` disables) are deduplicated by `(source_path, section)` keeping the latest version, rendered with provenance tags under the same token budget, and reported honestly in the coverage footer as `K/M FTS chunks`. In fallback mode, direct FTS chunks are not shown when graph retrieval produced at least one scored triple.

### 3.7 Context-Budgeted Retrieval (scoring, truncation, compaction)

**Design principle**: the SQLite store holds the full graph and raw evidence; `query` returns the most relevant slice within a caller-declared token budget, not everything reachable. Query seed extraction follows the configured extractor, but scoring, pruning, fusion, and budgeting are pure ranking plus deterministic token accounting.

#### Scoring

Every candidate triple *t* reached by BFS receives a composite score:

```
score(t) = decay^hops(t) × ( α · sim(t) + (1 − α) · bm25(t) )
```

- **`hops(t)`** — `hops(t) = min(depth(subject), depth(object))`, where seed entities have BFS depth 0; thus a relation incident to a seed entity has `hops(t) = 0`. The BFS lineage must be preserved into the result rather than discarded.
- **`decay`** — per-hop attenuation, default `0.7`, config field `retrieval.hop_decay`.
- **`sim(t)`** — cosine similarity between the query embedding and the embedding of the triple rendered as plain text (`"subject predicate object"`), normalised from \[−1, 1\] to \[0, 1\] via `(1 + cos) / 2`. The triple embedding is computed **once at ingest time** (when the relation is created or updated) and stored alongside the relation; this requires a relation-embedding column/table added by the M3 schema migration. Query time incurs exactly one embedder call — the query string itself. If no embedder is configured, `sim(t)` is omitted and its weight redistributes to `bm25` (i.e. α effectively becomes 0).
- **`bm25(t)`** — the maximum BM25 score (SQLite FTS5, from the M3 hybrid retrieval) over the evidence chunks attached to *t*, min-max normalised to \[0, 1\] within the current triple candidate set. **Important**: SQLite FTS5's `bm25()` function returns lower-is-better (negative) raw values; raw scores must be negated before min-max normalisation, otherwise ranking is inverted. Natural-language queries are converted to OR-of-tokens FTS5 MATCH expressions; phrase matching is too brittle for long questions. If all candidates share the same raw value (degenerate min-max, zero denominator), define `bm25(t) = 0` for all candidates. If a triple has no FTS-matching evidence, `bm25(t) = 0`.
- **`α`** — vector-vs-lexical mix, default `0.6`, config field `retrieval.alpha`.
- **Temporal**: superseded relations (M4) are excluded before scoring. No additional recency term in v1; using `doc_date` for confidence decay is listed as a future refinement.
- All defaults live on `MemboxConfig` in `RetrievalConfig` (`hop_decay`, `alpha`, `budget`, `top_evidence_k`, `fts_fallback_k`, `fusion_mode`, `chunk_share`) and are calibration targets for the Phase 7.5 gold-standard evaluation.
- **Deterministic tie-breaking**: when composite scores tie (notably the all-zero case where no embedder is configured and no FTS-matching evidence exists), order by `hops(t)` ascending, then by newest evidence (`doc_date` / `extracted_at`) descending. Ranking must be fully deterministic across identical inputs.

#### Token-budget truncation

- CLI and API: `membox query --budget <tokens>` (default `2000`, config `retrieval.budget`). Callers (e.g. the Phase 9 agent skill) declare how much context they are willing to spend.
- **Deterministic token estimator** (no tokeniser dependency):

  ```
  est_tokens(s) = (# CJK chars in s) + ceil((# non-CJK chars) / 4)
  ```

  Documented as an approximation; consistency matters more than accuracy.

- **Greedy fill**: within each candidate pool, add each item if its estimated cost fits the remaining budget, else skip it and continue down the list (best-effort knapsack, not first-fit-stop). Stop when the list is exhausted or the remaining budget falls below a minimum item size.
- Graph items have two granularities with separate costs:
  - The **triple line** itself (cheap).
  - Its **attached evidence snippet** (expensive). An evidence snippet is only eligible for the knapsack if its parent triple was already admitted within budget. Evidence is only ever attached for the top-*K* scored triples (*K* default `3`, config `retrieval.top_evidence_k`). Each evidence snippet is the markdown section chunk it came from (M2 chunking), never the whole document.
- FTS chunk items are provenance-tagged document section chunks. Their cost is the provenance tag plus chunk content.
- The coverage footer's own cost (a constant ~20 tokens) is excluded from the budget calculation — it is always appended regardless.
- Because triples are grouped by subject at render time, actual output may be slightly less than the sum of per-item token estimates (the estimator is conservative); this is acceptable.

#### Budget-partitioned fusion

In merge mode, the compact renderer uses three deterministic passes:

1. **Triple pass** — admit scored graph triple lines and top-K graph evidence within `budget - floor(budget * chunk_share)`.
2. **Chunk pass** — admit direct FTS chunks within `floor(budget * chunk_share)` plus unused triple-pass budget. Chunks whose `doc_id` was already printed as graph evidence are skipped.
3. **Triple backfill pass** — if the chunk pass leaves budget unused, admit additional graph triple lines without retrying evidence.

This keeps graph and FTS ranking independent while still sharing the caller's token budget. Oversized items are skipped, so a chunk pool can report `0/L FTS chunks` when no chunk fits the available chunk allowance.

#### Compact output format

- Triples are grouped by subject to avoid repeating entity names:

  ```
  membox: uses SQLite | has_phase 7.5 | next_step merge-branches
  ```

- Within each subject group, predicates are ordered by score descending.
- An evidence block (top-*K* triples only) is printed after the triple groups, each entry tagged with `project / source_path / section / doc_date` provenance, e.g.:

  ```
  [membox docs/HANDOFF.md ## Current state 2026-06-09]
  ```

- In merge mode, admitted direct FTS chunks are printed under a `Relevant source chunks` section with the same provenance tag format.

- A trailing one-line footer reports coverage honestly:

  ```
  (returned 18/42 triples, 4/5 FTS chunks, ~1,950/2,000 tokens; raise --budget for more)
  ```

  Silent truncation is forbidden; the caller must be able to see that more results exist.

#### Evaluation tie-in (M3)

`scripts/eval_memory.py` reports, per gold question: **hit/miss** AND **output token estimate**. Acceptance is judged on both dimensions: hit rate ≥ 80% within the default 2000-token budget (i.e. recall must be achieved while staying cheap, not by dumping everything).

### 3.8 Supersession Semantics

When the same source document is re-ingested with updated content, old relations derived from that source may become stale. Membox tracks this via a self-referencing `superseded_by` foreign key on the `relations` table:

- A relation is **active** when `superseded_by IS NULL`.
- When a newer document version produces a relation with the same subject + predicate but a different object, the old relation is marked `superseded_by = <new_relation_id>`.
- Retrieval excludes superseded relations by default; `--include-superseded` exposes them for auditing.
- Evidence rows are never deleted — the full provenance trail is always recoverable.

### 3.9 Asynchronous Ingestion (write path)

#### Motivation

Ingestion is LLM-bound: per-chunk extraction plus embedding takes tens of seconds to minutes per document. Callers — coding agents saving memory at session end — must not block on that. Write acceptance must be milliseconds; knowledge-graph materialization is deferred. Reads become eventually consistent, which is acceptable for the memory use case and must be observable (silent staleness is forbidden, consistent with the truncation footer principle in §3.7).

#### Design: queue in SQLite + auto-spawned short-lived worker

No resident daemon. This is a direct application of the project's "no background services by default" constraint: the worker is a transient subprocess that exits when the queue is empty. It is not a daemon — its lifetime is bounded to draining one batch. Processes that need the old blocking behavior use `--sync`; everything else returns immediately with a queue id.

The queue lives in the same SQLite file (WAL mode already supports concurrent writers; no second storage system).

#### Queue table (`ingest_queue`, schema migration v4)

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `content` | `TEXT NOT NULL` | Raw document text; chunking and extraction happen in the worker |
| `project` | `TEXT` | Captured at enqueue time |
| `source_path` | `TEXT` | Captured at enqueue time |
| `doc_date` | `TEXT` | Captured at enqueue time |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | `pending \| processing \| done \| failed` |
| `retries` | `INTEGER NOT NULL DEFAULT 0` | |
| `error` | `TEXT` | Last failure message |
| `enqueued_at` | `TEXT NOT NULL` | |
| `started_at` | `TEXT` | |
| `finished_at` | `TEXT` | |

The `worker_lease` key is added to the existing `meta` table (pid + hostname + heartbeat timestamp).

#### Enqueue path (fast)

`membox ingest`, `membox ingest-file`, and `MemoryAgent.ingest*` become: INSERT into `ingest_queue` + spawn worker if none is alive → return queue id immediately. No LLM calls and no chunking occur on this path. CLI prints the queue id and the current pending count.

- `--no-spawn` — suppresses worker spawn; useful in tests and controlled runs where the caller starts the worker explicitly.
- `--sync` — preserves the old blocking behavior (enqueue then drain inline) for scripts and evaluation pipelines that require determinism. `scripts/eval_memory.py` uses this path.

#### Worker (`membox process`)

Drain loop: claim one pending row at a time via an atomic `UPDATE … WHERE status='pending'` (pending → processing); run the existing M2/M3 pipeline (chunk → extract → embed → store); mark `done` or `failed` (record error message, increment `retries`). Exit when no pending rows remain.

Failed rows stay inspectable. They are retried only via `membox process --retry-failed` (maximum 3 retries; after that the row is permanently failed until manual intervention).

**Single-worker guarantee**: a lease entry in `meta` (key `worker_lease`) encodes pid + hostname + heartbeat timestamp. The worker refreshes the heartbeat after processing each item; lease TTL is ~60 s. A spawner that observes a live lease does not start a second worker. A worker that observes an expired lease takes over ownership and resets any stale `processing` rows back to `pending` (crash recovery).

The worker process is spawned with `start_new_session=True` (detached); its stdout and stderr are appended to a log file adjacent to the database (`<db>.worker.log`).

#### Observability

- `membox queue` — prints row counts per status and the most recent failure entries with their error messages.
- `membox query` — if `pending + processing > 0`, the coverage footer appends a note such as `(N ingests pending — results may be incomplete)`. This keeps the silent-staleness guarantee from §3.7 intact across the async boundary.

#### Eval and test integration

`scripts/eval_memory.py` uses the `--sync` path — determinism matters more than latency there. A dedicated unit test asserts the async properties: enqueue returns in <100 ms (API layer, excluding interpreter startup, `DummyExtractor` wired to a latency stub), and the queue drains to zero when `membox process` is run.

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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES entities(id),
    target_id      INTEGER NOT NULL REFERENCES entities(id),
    predicate      TEXT    NOT NULL,
    superseded_by  INTEGER REFERENCES relations(id),  -- NULL = active; set when a newer version supersedes this relation
    UNIQUE(source_id, target_id, predicate)  -- Triple deduplication
);

-- Documents (Raw Text + Scoping Metadata)
CREATE TABLE documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    content      TEXT    NOT NULL,
    source       TEXT,                        -- Source identifier (file path, URL, etc.)
    project      TEXT,                        -- Repository / directory name (for --project scoping)
    source_path  TEXT,                        -- Canonical file path of the originating document
    section      TEXT,                        -- Section heading (e.g. "## Summary") if chunked by heading
    doc_date     TEXT,                        -- ISO-8601 date of the source document snapshot
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Relation-Document Evidence (Many-to-Many)
CREATE TABLE relation_evidence (
    relation_id  INTEGER NOT NULL REFERENCES relations(id),
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    PRIMARY KEY (relation_id, document_id)
);

-- Database Metadata (written once at creation; mismatch triggers a clear error)
CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
-- Rows: embedding_model, embedding_dimensions
```

### 4.3 Core Modules

```
src/membox/
├── __init__.py          # Package entry point, exposes public APIs
├── config.py            # MemboxConfig — provider/base_url/API-key/model selection per capability
├── model/
│   └── schema.py        # Pydantic model definitions
├── core/
│   ├── store/           # SQLite storage layer, split by concern
│   │   ├── __init__.py  # KnowledgeStore facade (stable public method surface)
│   │   ├── connection.py  # Per-thread connections, WAL/PRAGMAs, transactions, RLock
│   │   ├── migrations.py  # PRAGMA user_version schema migration machinery
│   │   ├── entities.py    # Entity CRUD + find-or-create dedup + aliases
│   │   ├── relations.py   # Relation CRUD + evidence links
│   │   ├── documents.py   # Document persistence
│   │   ├── retrieval.py   # BFS multi-hop retrieval
│   │   └── queue.py       # ingest_queue table CRUD + worker lease management (M6)
│   ├── normalize.py     # Predicate normalization and synonym dictionary
│   ├── agent.py         # MemoryAgent — Orchestration layer
│   └── worker.py        # Queue drain loop + crash recovery (M6; spawned as subprocess)
├── services/            # Domain capability layer (never speaks HTTP directly)
│   ├── extraction.py    # LLMExtractor Protocol + Dummy/OpenAI implementations
│   ├── embedding.py     # Embedder Protocol + Dummy/OpenAI implementations
│   ├── ast_parser.py    # Tree-sitter code analysis (optional module)
│   └── prompts/
│       └── extraction.py  # Extraction prompt templates (module-level constants)
├── providers/           # Protocol adapter layer (auth, request shape, error normalization only)
│   ├── base.py          # ChatClient / EmbedClient low-level Protocols
│   └── openai_compat.py # OpenAI-compatible adapter (OpenAI/Ollama/vLLM/DeepSeek via base_url)
├── cli/                 # Typer CLI (presentation only)
│   ├── __init__.py      # App assembly; exposes the `app` entry point
│   └── commands/        # One module per command group (ingest / query / listing / version / queue / process)
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
| Service/Adapter Layering | `services/` (domain capabilities) over `providers/` (protocol adapters) | Services own prompts, parsing, and fallback policy and never speak HTTP; providers own auth, request shape, and error normalization only — adding a new backend (e.g. Gemini) touches `providers/` plus config, not domain logic. |
| Schema Migrations | `PRAGMA user_version` + ordered `MIGRATIONS` list | Each open applies pending migrations transactionally and bumps `user_version`; migration 0001 is the full idempotent DDL (`CREATE TABLE IF NOT EXISTS`) so pre-migration databases pass through unchanged. |
| Single Global DB | Default `~/.membox/membox.db`; override via `--db` flag or `MEMBOX_DB` env var | Entities and relations are global (enabling cross-project queries); documents are scoped by `project` / `source_path` columns. No per-project files, no ATTACH federation. |
| Embedding Model Guard | `meta` table stores `embedding_model` + `embedding_dimensions` at creation | On open, mismatch between stored and configured values fails with a clear error instructing re-embedding, preventing vector space corruption (e.g. 768-dim vs 1536-dim). |
| Disambiguation Threshold | `MemboxConfig.disambiguation_threshold` (default 0.85) | Threshold is provider-dependent: 0.85 for OpenAI; 0.70 preset for `embeddinggemma` via Ollama. Calibrated empirically — same-entity pairs 0.70–0.92, different-entity pairs 0.53–0.61 on embeddinggemma. |
| Supersession (No-Delete) | `relations.superseded_by` FK; old rows marked, never deleted | Preserves full provenance trail; retrieval filters active relations by default; `--include-superseded` enables auditing. |
| Hybrid Retrieval | FTS5 BM25 fused with graph BFS | SQLite FTS5 is zero-dependency; covers natural-language documents where entities are implicit and pure graph recall falls short. |

## 5. Interface Design

### 5.1 CLI Commands (Primary interface for coding agents)

Agents learn to use the following commands via the skill file without needing to understand the Python API:

```bash
# Ingest text
membox ingest "codebase-rag is implemented in Python" --source "README.md"

# Ingest file (with optional project scoping metadata)
membox ingest-file docs/architecture.md --db memory.db
membox ingest-file docs/HANDOFF.md --project myrepo --doc-date 2026-06-09

# Query memory (scoped to a project, or global when --project is omitted)
membox query "What technologies are used in the project?" --max-hops 2
membox query "What is the current state of the auth refactor?" --project myrepo

# List entities / relations (optional --project filter)
membox list-entities --db memory.db
membox list-relations --db memory.db --project myrepo

# Analyze source structure (tree-sitter, optional)
membox analyze-src src/ --language python --db memory.db

# Async ingestion queue (M6)
membox ingest-file docs/HANDOFF.md --project myrepo  # enqueues and spawns worker; returns in <100ms
membox ingest-file large.md --sync                   # blocking mode: enqueue + drain inline
membox ingest-file large.md --no-spawn               # enqueue only; caller starts worker manually
membox process                                        # drain the queue; exits when empty
membox process --retry-failed                         # retry failed rows (up to 3 total attempts)
membox queue                                          # show per-status counts and recent failures
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
- Ollama (local server, not a Python package) — Enables `embeddinggemma` (768-dim) and `huihui_ai/qwen3.5-abliterated:4b-Claude` extraction without an API key; accessed via the `openai_compat` provider adapter pointed at `http://localhost:11434`

### Development

- `pytest >= 8` / `pytest-cov >= 6` — Testing
- `ruff >= 0.11` — lint + format
- `mypy >= 1.15` — Type Checking
- `pre-commit >= 4` — Git hooks

## 8. Extension Roadmap

### Planned (see roadmap.md)

| Target | Description | Phase |
|------|------|------|
| Memory Quality Validation | Real-corpus evaluation on handoff documents; hybrid retrieval; supersession semantics; local Ollama provider defaults. | Phase 7.5 (M1–M3, M5) |
| Async Ingestion Queue | Decouple write acceptance from LLM materialization; transient worker; crash recovery via lease; `process` and `queue` CLI commands. | Phase 7.5 M6 |
| Skill File | Skill instruction documents to teach agents how to use the CLI. | Phase 9 |
| Codebase Analysis | Multi-language AST parsing using tree-sitter to extract module dependencies, call graphs, and class structures. | Phase 8 |

### Future Options

| Direction | Description | Trigger Condition |
|------|------|----------|
| Vector Index Upgrade | Replace `find_similar_entity` with sqlite-vss, FAISS, or Lance. | Entity count > ~100k |
| Automatic Predicate Clustering | Automatic synonym predicate discovery via embedding clustering. | Predicate type explosion |
| Confidence & Auditing | Add `confidence` / `merged_from` fields to entities. | When human-in-the-loop review is required |
| Temporal Confidence Decay | Add a `confidence` score to `relation_evidence` that decays over time for time-sensitive facts. | Requirements beyond supersession (e.g. probabilistic staleness scoring) |
