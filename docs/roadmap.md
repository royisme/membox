# Membox — Implementation Roadmap

> Based on [spec.md](./spec.md). Interface-first, top-down: first build the complete skeleton (CLI → Agent → Protocols of submodules), then implement each module one by one.

## Phase 0 — Project Skeleton ✅

Scaffolding and runtime dependencies are ready.

- [x] `pyproject.toml` configuration (typer, rich, pydantic)
- [x] pre-commit hooks
- [x] GitHub Actions CI
- [x] CLI entry point registration (`membox` command is available)
- [x] Minimal `cli.py` (`version` command)
- [x] Optional dependency groups (`openai`, `tree-sitter`)

## Phase 1 — Complete Framework Skeleton

**Goal**: Every module, function signature, and protocol from CLI down to the lowest layer is in place. Function bodies can be stubs, but the **import chain must be fully connected**, and `membox --help` output lists all commands.

```
cli.py                    ← Typer commands, each calling the agent
  └→ agent.py             ← MemoryAgent class orchestrating submodules
       ├→ schema.py       ← Pydantic models (Entity, Relation, Document, Triple, HopResult)
       ├→ store.py        ← KnowledgeStore class (Protocol + stub methods)
       ├→ normalize.py    ← normalize_predicate() (stub)
       ├→ extract.py      ← LLMExtractor Protocol + DummyExtractor
       └→ embed.py        ← Embedder Protocol + DummyEmbedder
```

### 1.1 Data Models `schema.py`

- [ ] `Entity` — Entity model (id, name, type, embedding, created_at)
- [ ] `EntityAlias` — Alias model (entity_id, alias)
- [ ] `Relation` — Relation model (id, source_id, target_id, predicate)
- [ ] `Document` — Document model (id, content, source, created_at)
- [ ] `Evidence` — Evidence model (relation_id, document_id)
- [ ] `Triple` — Extraction output (source, predicate, target, source_type, target_type)
- [ ] `HopResult` — BFS single-hop output (entity, relation, via_entities, evidences)

### 1.2 Protocol Definitions `protocols` (Scattered across modules)

- [ ] `store.py` — `KnowledgeStore` class with all method signatures (stub implementation)
- [ ] `extract.py` — `LLMExtractor` Protocol + full `DummyExtractor` implementation
- [ ] `embed.py` — `Embedder` Protocol + full `DummyEmbedder` implementation
- [ ] `normalize.py` — `normalize_predicate()` stub

### 1.3 Orchestration Layer `agent.py`

- [ ] `MemoryAgent.__init__(store, extractor, embedder, db_path)`
- [ ] `ingest(text, source)` — Call extractor → normalize → store.find_or_create → store.add_relation
- [ ] `query(question, max_hops)` — Call store.bfs_query → assemble prompt
- [ ] `list_entities()` / `list_relations()` — Proxy to store

### 1.4 CLI Layer `cli.py`

- [ ] `membox ingest` — Read text, call agent.ingest
- [ ] `membox ingest-file` — Read file, call agent.ingest
- [ ] `membox query` — Pass query, call agent.query
- [ ] `membox list-entities` — Call agent.list_entities, output via rich table
- [ ] `membox list-relations` — Call agent.list_relations, output via rich table
- [ ] All commands support `--db` / `--help`

### 1.5 Exporting `__init__.py`

- [ ] Export public APIs such as `MemoryAgent`, `OpenAIExtractor`, and `OpenAIEmbedder`

**Validation**:
- `uv run membox --help` outputs the complete command list
- `uv run mypy src` completes with zero errors (all signatures fully typed)
- `uv run pytest tests/` — skeleton tests pass (stubs do not crash)
- **Import chain is fully connected from cli → agent → store/extract/embed**

## Phase 2 — Storage Implementation

**Goal**: Populate all stub methods in `KnowledgeStore` with real implementations.

- [ ] Table schema DDL (entities, entity_aliases, relations, documents, relation_evidence)
- [ ] Enable `PRAGMA foreign_keys=ON` + WAL mode
- [ ] Entity CRUD: `insert_entity` / `find_entity_by_name` / `list_entities`
- [ ] Alias CRUD: `add_alias` / `find_entity_by_alias`
- [ ] Relation CRUD: `insert_relation` (deduplication via UNIQUE) / `list_relations`
- [ ] Document CRUD: `insert_document` / `get_document`
- [ ] Evidence CRUD: `add_evidence` / `get_evidence_for_relation`
- [ ] Tests: table creation, foreign key constraints, triple UNIQUE deduplication, and evidence many-to-many relationship

**Validation**: Running the CLI command `membox ingest "test"` writes to SQLite, and `membox list-entities` reads from it.

## Phase 3 — Predicate Normalization

**Goal**: Populate `normalize_predicate()` with a real implementation.

- [ ] Built-in English/Chinese synonym dictionary (e.g., `developed`/`develop`/`开发` → `develops`)
- [ ] Lowercase normalization + dictionary lookup, falling back to original lowercased string on miss
- [ ] Tests: English/Chinese synonyms + pass-through for unknown predicates

**Validation**: `membox ingest "A 开发了 B"` → relation predicate is stored as `develops`.

## Phase 4 — Entity Disambiguation

**Goal**: Populate `find_or_create_entity()` with the three-tier cascading disambiguation strategy.

- [ ] Exact alias matching
- [ ] Cosine similarity matching (cosine ≥ 0.85) for entities of the same type
- [ ] Fallback: Create new entity
- [ ] String-only fallback (when no embedder is provided: exact match + casing normalization)
- [ ] Tests
  - [ ] String exact / casing deduplication
  - [ ] Embedding synonym deduplication
  - [ ] Negative test: unrelated entities are not merged
  - [ ] Concurrent identical entities (8 threads calling `find_or_create` concurrently, resulting in exactly 1 record)

**Validation**: Ingesting the same entity repeatedly does not create duplicate records.

## Phase 5 — Multi-hop Retrieval

**Goal**: Populate `bfs_query()` with a real implementation.

- [ ] BFS expansion starting from seed entities, with configurable `max_hops`
- [ ] Record paths, associated entities, relations, and source evidence for each hop
- [ ] Tests
  - [ ] 2-hop recalls C, does not recall D
  - [ ] 3-hop recalls D
  - [ ] Context aggregation (complete path reconstruction)
  - [ ] Provenance trace back to original source text

**Validation**: `membox query "What is the relationship between X and Y?" --max-hops 2` returns results with source evidence.

## Phase 6 — Concurrency Hardening

**Goal**: Multiple agents can write concurrently without conflict.

- [ ] Per-thread SQLite connections (`threading.local()`)
- [ ] SQLite WAL mode enabled (implemented in Phase 2)
- [ ] Use `RLock` to guard the critical section of `find_or_create_entity`
- [ ] Tests
  - [ ] Concurrent multi-threaded writes (5 threads × 10 writes, zero errors, accurate final counts)
  - [ ] Concurrent identical entities (verify the correctness of `RLock`)

**Validation**: Concurrent tests run without errors or deadlocks.

## Phase 7 — OpenAI Integration

**Goal**: Integrate real LLMs to replace the Dummy implementations.

- [ ] `src/membox/extract.py` — `OpenAIExtractor` implementation
- [ ] `src/membox/embed.py` — `OpenAIEmbedder` implementation
- [ ] `examples/demo.py` — End-to-end demo script
- [ ] Manual validation: ingest real documents → query returns meaningful results

**Validation**: `OPENAI_API_KEY=sk-... uv run python examples/demo.py` runs successfully.

## Phase 8 — Codebase Structural Analysis (tree-sitter)

**Goal**: Extract structural codebase knowledge using AST parsing.

- [ ] `src/membox/ast_parser.py`
  - [ ] Integrate tree-sitter, loading language grammar on demand
  - [ ] Extract structural triples: `module --defines--> class` / `class --has_method--> method` / `method --calls--> function`
  - [ ] CLI command: `membox analyze-src <path> --language <lang>`
- [ ] Python grammar support first
- [ ] Tests: Python source file parsing / module dependency graph / class structure

**Validation**: Running `membox analyze-src src/` on its own codebase, query successfully recalls module structures.

## Phase 9 — Skill Files

**Goal**: Write skill instruction files for coding agents.

- [ ] `skills/membox-skill.md` — Generic skill template
  - [ ] Installation instructions
  - [ ] Command reference
  - [ ] Usage examples
- [ ] Manual validation: agent reads the skill and successfully invokes the CLI commands

**Validation**: Inject the skill into agent context; the agent can independently perform ingest + query.

## Phase 10 — Polish and Release

- [ ] Update README.md
- [ ] Complete documentation (docstring coverage for all public APIs)
- [ ] Achieve test coverage target (≥ 80%)
- [ ] `uv run mypy src` completes with zero errors
- [ ] `uv run ruff check .` completes with zero warnings
- [ ] Bump version numbers

---

## Build Order

```
Phase 0 Skeleton ✅
    │
Phase 1 Complete Framework (Interface-first, all module stubs connected)
    │
    ├→ Phase 2 Storage Implementation
    ├→ Phase 3 Normalization Implementation
    ├→ Phase 4 Disambiguation Implementation ──→ Phase 6 Concurrency Hardening
    ├→ Phase 5 Multi-hop Retrieval Implementation
    │
    └→ Phase 7 OpenAI Integration
         │
         ├→ Phase 8 tree-sitter (Can be done in parallel)
         ├→ Phase 9 Skill Files (Can be done in parallel)
              │
              └→ Phase 10 Release
```

**Principle**: After Phase 1, each subsequent phase should only focus on one thing—**populating the stubs reserved in Phase 1**. Do not alter signatures, imports, or architecture.
