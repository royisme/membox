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
cli.py                         ← Typer commands, each calling the agent
  └→ core/agent.py             ← MemoryAgent class orchestrating submodules
       ├→ model/schema.py      ← Pydantic models (Entity, Relation, Document, Triple, HopResult)
       ├→ core/store.py        ← KnowledgeStore class (Protocol + stub methods)
       ├→ core/normalize.py    ← normalize_predicate() (stub)
       ├→ services/extraction.py ← LLMExtractor Protocol + DummyExtractor
       └→ services/embedding.py  ← Embedder Protocol + DummyEmbedder
```

### 1.1 Data Models `model/schema.py`

- [x] `Entity` — Entity model (id, name, type, embedding, created_at)
- [x] `EntityAlias` — Alias model (entity_id, alias)
- [x] `Relation` — Relation model (id, source_id, target_id, predicate)
- [x] `Document` — Document model (id, content, source, created_at)
- [x] `Evidence` — Evidence model (relation_id, document_id)
- [x] `Triple` — Extraction output (source, predicate, target, source_type, target_type)
- [x] `HopResult` — BFS single-hop output (entity, relation, via_entities, evidences)

### 1.2 Protocol Definitions `protocols` (Scattered across modules)

- [x] `core/store.py` — `KnowledgeStore` class with all method signatures (stub implementation)
- [x] `services/extraction.py` — `LLMExtractor` Protocol + full `DummyExtractor` implementation
- [x] `services/embedding.py` — `Embedder` Protocol + full `DummyEmbedder` implementation
- [x] `core/normalize.py` — `normalize_predicate()` stub

### 1.3 Orchestration Layer `core/agent.py`

- [x] `MemoryAgent.__init__(store, extractor, embedder, db_path)`
- [x] `ingest(text, source)` — Call extractor → normalize → store.find_or_create → store.add_relation
- [x] `query(question, max_hops)` — Call store.bfs_query → assemble prompt
- [x] `list_entities()` / `list_relations()` — Proxy to store

### 1.4 CLI Layer `cli.py`

- [x] `membox ingest` — Read text, call agent.ingest
- [x] `membox ingest-file` — Read file, call agent.ingest
- [x] `membox query` — Pass query, call agent.query
- [x] `membox list-entities` — Call agent.list_entities, output via rich table
- [x] `membox list-relations` — Call agent.list_relations, output via rich table
- [x] All commands support `--db` / `--help`

### 1.5 Exporting `__init__.py`

- [x] Export public APIs such as `MemoryAgent`, `OpenAIExtractor`, and `OpenAIEmbedder`

**Validation**:
- `uv run membox --help` outputs the complete command list
- `uv run mypy src` completes with zero errors (all signatures fully typed)
- `uv run pytest tests/` — skeleton tests pass (stubs do not crash)
- **Import chain is fully connected from cli → agent → store/extract/embed**

## Phase 2 — Storage Implementation

**Goal**: Populate all stub methods in `KnowledgeStore` with real implementations.

- [x] Table schema DDL (entities, entity_aliases, relations, documents, relation_evidence)
- [x] Enable `PRAGMA foreign_keys=ON` + WAL mode
- [x] Entity CRUD: `insert_entity` / `find_entity_by_name` / `list_entities`
- [x] Alias CRUD: `add_alias` / `find_entity_by_alias`
- [x] Relation CRUD: `insert_relation` (deduplication via UNIQUE) / `list_relations`
- [x] Document CRUD: `insert_document` / `get_document`
- [x] Evidence CRUD: `add_evidence` / `get_evidence_for_relation`
- [x] Tests: table creation, foreign key constraints, triple UNIQUE deduplication, and evidence many-to-many relationship

**Validation**: Running the CLI command `membox ingest "test"` writes to SQLite, and `membox list-entities` reads from it.

## Phase 3 — Predicate Normalization

**Goal**: Populate `normalize_predicate()` with a real implementation.

- [x] Built-in English/Chinese synonym dictionary (e.g., `developed`/`develop`/`开发` → `develops`)
- [x] Lowercase normalization + dictionary lookup, falling back to original lowercased string on miss
- [x] Tests: English/Chinese synonyms + pass-through for unknown predicates

**Validation**: `membox ingest "A 开发了 B"` → relation predicate is stored as `develops`.

## Phase 4 — Entity Disambiguation

**Goal**: Populate `find_or_create_entity()` with the three-tier cascading disambiguation strategy.

- [x] Exact alias matching
- [x] Cosine similarity matching (cosine ≥ 0.85) for entities of the same type
- [x] Fallback: Create new entity
- [x] String-only fallback (when no embedder is provided: exact match + casing normalization)
- [x] Tests
  - [x] String exact / casing deduplication
  - [x] Embedding synonym deduplication
  - [x] Negative test: unrelated entities are not merged
  - [x] Concurrent identical entities (8 threads calling `find_or_create` concurrently, resulting in exactly 1 record)

**Validation**: Ingesting the same entity repeatedly does not create duplicate records.

## Phase 5 — Multi-hop Retrieval

**Goal**: Populate `bfs_query()` with a real implementation.

- [x] BFS expansion starting from seed entities, with configurable `max_hops`
- [x] Record paths, associated entities, relations, and source evidence for each hop
- [x] Tests
  - [x] 2-hop recalls C, does not recall D
  - [x] 3-hop recalls D
  - [x] Context aggregation (complete path reconstruction)
  - [x] Provenance trace back to original source text

**Validation**: `membox query "What is the relationship between X and Y?" --max-hops 2` returns results with source evidence.

## Phase 6 — Concurrency Hardening

**Goal**: Multiple agents can write concurrently without conflict.

- [x] Per-thread SQLite connections (`threading.local()`)
- [x] SQLite WAL mode enabled (implemented in Phase 2)
- [x] Use `RLock` to guard the critical section of `find_or_create_entity`
- [x] Tests
  - [x] Concurrent multi-threaded writes (5 threads × 10 writes, zero errors, accurate final counts)
  - [x] Concurrent identical entities (verify the correctness of `RLock`)

**Validation**: Concurrent tests run without errors or deadlocks.

## Phase 7 — OpenAI Integration

**Goal**: Integrate real LLMs to replace the Dummy implementations.

- [x] `src/membox/services/extraction.py` — `OpenAIExtractor` implementation
- [x] `src/membox/services/embedding.py` — `OpenAIEmbedder` implementation
- [x] `examples/demo.py` — End-to-end demo script
- [x] Manual validation: ingest real documents → query returns meaningful results

**Validation**: `OPENAI_API_KEY=sk-... uv run python examples/demo.py` runs successfully.

## Phase 7.5 — Memory Quality Validation

**Goal**: Validate that membox functions as memory on real, evolving corpora before advancing to AST analysis. Phases 1–7 exercised mechanics with synthetic text only; this phase ingests session handoff documents to close three gaps: markdown-structured input, temporal evolution (handoffs are rewritten in place each session), and recall over long sentences where pure graph traversal may be insufficient.

### M1 — Evaluation Corpus & Gold Standard

- [ ] Snapshot ~10 real `HANDOFF.md` files from across the user's projects into `eval/corpus/`
- [ ] Hand-author 20–30 gold QA pairs covering three categories:
  - [ ] Single-hop facts
  - [ ] Multi-hop (cross-document / cross-project)
  - [ ] Temporal — "current state" questions whose answer must come from the **latest** document version

### M2 — Ingestion Hardening

- [ ] Markdown-aware chunking: split on `##` section boundaries before extraction
- [ ] Persist document metadata: `project`, `source_path`, `section`, `doc_date` (new columns on `documents`)
- [ ] Idempotent re-ingest: re-ingesting the same `source_path` creates a new document version instead of duplicating raw content
- [ ] CLI: `membox ingest-file` accepts and stores metadata fields; `--project` filter on query and listing commands

### M3 — Baseline Evaluation & Hybrid Retrieval

- [ ] `scripts/eval_memory.py`: ingest corpus → run gold questions → report per-category hit rates
- [ ] CI smoke variant: offline run using fake extractor/embedder (no Ollama required)
- [ ] Full evaluation: manual run against local Ollama, results recorded in `eval/results/`
- [ ] Measure graph-only recall baseline first
- [ ] Implement SQLite FTS5 BM25 over `documents.content`, fused with graph retrieval (promotes hybrid retrieval from spec's future-options list into committed scope)
- [ ] **Scored rerank**: implement composite scoring formula `score(t) = decay^hops(t) × (α·sim(t) + (1−α)·bm25(t))` with BFS lineage preservation; add `RetrievalConfig` group to `MemboxConfig` (`hop_decay=0.7`, `alpha=0.6`, `budget=2000`, `top_evidence_k=3`); schema migration adds a relation-embedding column so `sim(t)` is precomputed at ingest time (one embedder call per relation write, not per query); negate raw FTS5 `bm25()` values before min-max normalisation (SQLite returns lower-is-better negatives — inverting this is a known gotcha)
- [ ] **Token-budget truncation**: implement deterministic token estimator (`est_tokens(s) = CJK_count + ceil(non_CJK_count / 4)`); greedy best-effort knapsack fill sorted by score descending; expose `membox query --budget <tokens>` CLI flag
- [ ] **Compact subject-grouped output format**: group triples by subject with predicates ordered by score descending; print top-K evidence snippets with `project/source_path/section/doc_date` provenance tags; append honest coverage footer `(returned N/M triples, ~X/Y tokens; raise --budget for more)` — silent truncation is forbidden
- [ ] **Eval metric — output token count**: extend `scripts/eval_memory.py` to report both hit/miss and output token estimate per gold question; acceptance criterion is hit rate ≥ 80% *within* the default 2000-token budget

### M4 — Supersession Semantics (Schema Migration)

- [ ] `relations` table gains nullable `superseded_by` column (FK → `relations.id`, self-referencing)
- [ ] When a newer version of the same source document yields a relation with the same subject + predicate but a different object, the old relation is marked `superseded_by = <new_relation_id>`
- [ ] Retrieval excludes superseded relations by default
- [ ] `--include-superseded` flag on `membox query` exposes them for auditing
- [ ] Old evidence is never deleted
- [ ] Migration delivered via existing `PRAGMA user_version` mechanism (no registry, no schema reset)

### M5 — Close the Loop

- [ ] `membox ingest-file docs/HANDOFF.md` works end-to-end against local Ollama
- [ ] This becomes the substrate for Phase 9's skill file (query at session start, ingest at session end)

### M6 — Asynchronous Ingestion Queue

> **Implementation order note**: M6 is implemented next — before M4 and M5. Coding agents call `ingest` at session end and cannot block on LLM extraction. M6 decouples write acceptance from knowledge-graph materialization so that call-site latency is bounded regardless of document size. M4 (supersession semantics) and M5 (end-to-end close-the-loop) will build on the async write path being stable, so M6 is the logical prerequisite. Revised sequence: **M6 → M4 → M5**.

Ingestion is LLM-bound: per-chunk extraction plus embedding can take tens of seconds to minutes per document. Write acceptance must be milliseconds; knowledge-graph materialization is deferred to a short-lived worker process. Reads are eventually consistent — acceptable for the memory use case — but staleness is never silent.

**No resident daemon.** This is consistent with the project's "no background services by default" constraint. The worker is a transient subprocess that exits when the queue is empty, not a daemon; its lifetime is bounded to the drain of one batch.

#### Queue table (schema migration v4)

New table `ingest_queue` in the same SQLite file (WAL mode already supports concurrent writers; no second storage system):

- `id INTEGER PRIMARY KEY`
- `content TEXT NOT NULL` — raw document text; all chunking and extraction happen in the worker
- `project TEXT`, `source_path TEXT`, `doc_date TEXT` — metadata captured at enqueue time
- `status TEXT NOT NULL DEFAULT 'pending'` — `pending | processing | done | failed`
- `retries INTEGER NOT NULL DEFAULT 0`, `error TEXT`
- `enqueued_at TEXT NOT NULL`, `started_at TEXT`, `finished_at TEXT`

#### Enqueue path (fast)

`membox ingest` / `ingest-file` and `MemoryAgent.ingest*` become: INSERT into `ingest_queue` + spawn worker if none is alive → return queue id immediately. No LLM calls, no chunking on this path. CLI prints the queue id and the pending count.

- `--no-spawn` suppresses worker spawn (for tests and controlled runs).
- `--sync` preserves the old blocking behavior (enqueue + drain inline) for scripts and eval runs that require determinism. `scripts/eval_memory.py` uses this path.

#### Worker (`membox process`)

Drain loop: claim one pending row at a time via atomic `UPDATE … WHERE status='pending'` (pending → processing); chunk → extract → embed → store (existing M2/M3 pipeline); mark `done` or `failed` (record error, increment `retries`). Exits when no pending rows remain.

Failed rows stay inspectable; they are retried only via explicit `membox process --retry-failed` (maximum 3 retries, then permanently failed until manual intervention).

**Single-worker guarantee** via a lease entry in the `meta` table (key `worker_lease`, value encodes pid + hostname + heartbeat timestamp). The worker refreshes its heartbeat after each item; lease TTL is ~60 s. A spawner that finds a live lease does not spawn a second worker. A worker that finds an expired lease takes over and resets any stale `processing` rows back to `pending` (crash recovery).

Worker is spawned as a detached subprocess (`start_new_session=True`); its stdout and stderr are appended to a log file next to the database (e.g. `<db>.worker.log`).

#### Observability

- `membox queue` — prints counts per status plus the most recent failures with their error messages.
- `membox query` — if `pending + processing > 0`, the coverage footer gains a note such as `(N ingests pending — results may be incomplete)`. Silent staleness is forbidden, consistent with the truncation footer principle.

#### Eval integration

`scripts/eval_memory.py` uses the `--sync` semantics — determinism matters more than latency there. A dedicated test asserts the async path: enqueue returns in <100 ms with a `DummyExtractor` wired to a slow stub, and the queue drains correctly when `membox process` runs.

#### Acceptance criteria (M6)

- [x] Schema migration v4: `ingest_queue` table created, `worker_lease` key in `meta`
- [x] `membox ingest` / `ingest-file` returns in <100 ms for a 5 KB markdown file (measured at the API layer, excluding interpreter startup; `DummyExtractor` wired)
- [x] `--sync` flag: enqueue + drain complete inline before the call returns; `eval_memory.py` uses this path
- [x] `--no-spawn` flag: enqueue without spawning worker (queue id returned; worker started manually via `membox process`)
- [x] `membox process` drains the queue then exits (asserted in a test — no daemon)
- [x] `membox queue` prints per-status counts and recent failure details
- [x] `membox query` coverage footer includes a pending-ingests note when the queue is non-empty
- [x] Crash recovery: a killed worker's `processing` rows return to `pending`; a new worker completes them
- [x] `uv run pytest` + ruff + mypy green; coverage ≥ 80%

**Validation**:

- Gold-standard hit rate ≥ 80% overall; temporal-category questions 100%
- Memory evaluation (`scripts/eval_memory.py`) reports both hit rate and output token estimate; acceptance requires hit rate ≥ 80% within the default 2000-token budget (recall achieved without dumping everything)
- Re-ingesting an updated handoff changes the answers to "current state" questions
- `uv run pytest` + ruff + mypy green; coverage ≥ 80%

---

## Lifecycle Track — Phases A–F (spec_02)

Normative source: `docs/spec/spec_02_memory_lifecycle.md`. Per-phase plans: `docs/plans/`.

| Phase | Description | Status |
|-------|-------------|--------|
| A | Retrieval groundwork | ✅ |
| B | History trace index, migration 6 | ✅ |
| C | Triage + memory units, migration 8, heuristic gate | ✅ |
| D | Consolidation, crystal policy, gate v3 | ✅ |
| E | Query fusion `--include-memory`, merged f274d27 | ✅ |
| F | Distill workflows | ✅ merged, plan_06 |

---

## Stabilization Track — v0.1.0 Release (current focus)

> Owner decision 2026-06-12: Phases A–F of the Lifecycle Track are complete (merged via PR #5). The next milestone is stabilization and release of the current feature set. Phase 8 (tree-sitter AST) is deferred until after v0.1.0.

| Step | Description | Status |
|------|-------------|--------|
| S1 | Dogfooding | 🔲 |
| S2 | Robustness | 🔲 |
| S3 | Deferred review items (PR #5 / plan_06) | 🔲 |
| S4 | Performance sanity | 🔲 |
| S5 | Release | 🔲 |

### S1 — Dogfooding

End-to-end run of the full Lifecycle Track pipeline on membox's own dev sessions:

- [ ] `membox history import` on real `~/.codex/sessions` (or Claude history) for the membox project
- [ ] `membox process` — drain the async queue
- [ ] `membox memory triage --apply` — classify new traces
- [ ] `membox memory extract --apply` — extract memory units
- [ ] `membox memory consolidate --apply` — promote crystals, supersede stale units
- [ ] `membox distill` — generate a distilled Markdown export
- [ ] `membox query --include-memory "..."` — confirm memory surfaces in answers
- [ ] File defects / gaps discovered during the run as GitHub issues

### S2 — Robustness

Harden the import and processing pipeline against real-world edge cases:

- [ ] Corrupt or partial JSONL lines — graceful skip with `failures` log entry, no worker crash
- [ ] Idempotent re-import — re-running `history import` on already-imported sessions produces no duplicate rows
- [ ] Interrupted worker recovery — a killed `membox process` leaves no stale `processing` rows after the next worker start
- [ ] Empty or missing session roots — `membox history import` with an absent `--root` reports the path and exits cleanly, not a traceback

### S3 — Deferred Review Items (PR #5 / plan_06)

Fold in the items deferred during plan_06 code review:

- [ ] Consolidate CLI N+1 source counts — batch `count_independent_sources_for_units` calls in `membox memory consolidate` output (same pattern as Phase E's `count_independent_sources_for_units` fix)
- [ ] Atomic apply batching — wrap the per-transition apply loop in a single transaction with abort reporting (current: per-transition commits; lease + status log mitigate but a mid-apply crash leaves partial state)
- [ ] FTS-based conflict candidate pairing — replace the in-memory pairwise scan in the conflict detector with an FTS5 candidate query to bound quadratic blowup on large unit sets
- [ ] LLM conflict comparator — injectable `ConflictComparator` Protocol backed by an LLM call; replaces the deterministic word-list signal for high-confidence conflict detection (real-trace recall currently uncalibrated)
- [ ] Gate v4 — `--help-dump` event family: add a heuristic rule to suppress bare `--help` output events from triage extraction (accepted residual risk in plan_04 D0; gate bump re-triages existing pending rows)

### S4 — Performance Sanity

Baseline timing on a representative real history:

- [ ] Import + process a large real session history (≥ 50 sessions / ≥ 500 messages) and record wall-clock time
- [ ] Document baseline in `eval/results/perf-baseline-v0.1.0.md`: import time, process time, queue drain time, entity/relation counts
- [ ] Identify and file any regressions vs plan_03 ingest-perf benchmarks (embed cache, batch embedder, `MEMBOX_INGEST_CONCURRENCY`)

### S5 — Release

- [ ] README quickstart covering the history-pull → process → distill → query workflow (end-to-end in ~10 commands)
- [ ] Changelog: `uv run python scripts/generate_changelog.py --version 0.1.0`
- [ ] Version bump: `uv run python scripts/bump_version.py 0.1.0`
- [ ] Tag `v0.1.0` and push; confirm CI green on main
- [ ] Close or re-milestone any open GitHub issues that are out of scope for v0.1.0

---

## Phase 8 — Codebase Structural Analysis (tree-sitter)

> **Deferred** (owner decision 2026-06-12): Phase 8 is deferred to the next milestone. Stabilization and v0.1.0 release of the current feature set take priority. The Stabilization Track below is the active focus.

> **Note**: Phase 8 is deliberately sequenced after Phase 7.5. The supersession semantics and hybrid retrieval introduced in 7.5 are load-bearing for the temporal recall patterns that codebase analysis will exercise.

**Goal**: Extract structural codebase knowledge using AST parsing.

- [ ] `src/membox/services/ast_parser.py`
  - [ ] Integrate tree-sitter, loading language grammar on demand
  - [ ] Extract structural triples: `module --defines--> class` / `class --has_method--> method` / `method --calls--> function`
  - [ ] CLI command: `membox analyze-src <path> --language <lang>`
- [ ] Python grammar support first
- [ ] Tests: Python source file parsing / module dependency graph / class structure

**Validation**: Running `membox analyze-src src/` on its own codebase, query successfully recalls module structures.

## Phase 9 — Skill Files

**Goal**: Write skill instruction files for coding agents.

- [x] `skills/membox-skill.md` — Generic skill template
  - [x] Installation instructions
  - [x] Command reference
  - [x] Usage examples
- [x] Manual validation: agent reads the skill and successfully invokes the CLI commands — delivered PR #5 (2026-06-12)

**Validation**: Inject the skill into agent context; the agent can independently perform ingest + query.

## Phase 10 — Polish and Release

- [x] Update README.md
- [x] Complete documentation (docstring coverage for all public APIs)
- [x] Achieve test coverage target (≥ 80%)
- [x] `uv run mypy src` completes with zero errors
- [x] `uv run ruff check .` completes with zero warnings
- [x] Bump version numbers

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
         └→ Phase 7.5 Memory Quality Validation
              │  (M1+M2+M3 complete → M6 async queue → M4 supersession → M5 close-the-loop)
              │
              └→ Lifecycle Track A–F ✅ (spec_02)
                   │
                   └→ Stabilization Track S1–S5 ← current focus
                        │
                        ├→ Phase 8 tree-sitter (deferred — next milestone)
                        ├→ Phase 9 Skill Files ✅ (merged PR #5)
                             │
                             └→ Phase 10 Release
```

**Principle**: After Phase 1, each subsequent phase should only focus on one thing—**populating the stubs reserved in Phase 1**. Do not alter signatures, imports, or architecture.

---

## Future Tracks (not yet scheduled)

- **Agent memory lifecycle** (Trace → Unit → Crystal): design accepted at v2.3
  in `docs/spec/spec_02_memory_lifecycle.md`. Promoted into this roadmap as the
  Lifecycle Track (Phases A–F); see the "Lifecycle Track — Phases A–F" section
  above. Phases A–E implemented; Phase F merged.
- **HOT working-state tier** (current task, open loops, session focus):
  explicitly excluded from the lifecycle track (see its Rejected Alternatives —
  working state has opposite mechanics from long-term memory). Recorded here as
  a future standalone design, owned by a separate doc when picked up; it must
  not be implemented as a `memory_units.unit_type`.
- **Global memory scope** (cross-project distillation): the surviving idea from
  the retired `docs/spec-v0.2-draft.md` (owner decision 2026-06-13 — the draft's
  per-project DB storage model was rejected; single global DB stays locked).
  Goal: distill reusable cross-project memory (user preferences, workflows,
  problem-solving procedures) from per-project memory units into a global
  scope, with provenance back to the source units. Builds ON TOP of the
  lifecycle track — depends on Phases C–F (`memory_units` with sources/labels
  give it the substrate the draft lacked); likely realized as a `project=''`
  or dedicated-scope query surface plus a `memory distill` command, NOT as a
  second DB file. Owns its own `docs/spec/spec_NN_global_scope.md` chapter
  when picked up.
