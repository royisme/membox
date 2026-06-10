# Membox — Project Specification v0.2 (Draft)

> **Version**: 0.2.0-draft · **Status**: Draft — Pending Review · **License**: MIT
> **Replaces**: `spec.md` 0.1.0 (Contents of this file will be merged into `docs/spec.md` once reviewed and approved)
> **Compatibility**: All v0.1 public APIs (`ingest` / `query` / `list-*`) and database schemas remain compatible; v0.2 introduces incremental extensions and one controlled schema migration.

---

## 0. Motivation for Revision

v0.1 positioned Membox as a "Knowledge Graph + RAG retrieval layer". The actual goal, however, is a **hierarchical memory system with a full lifecycle**:

1. **Two-level Memory** — Divided into Global (general, cross-project memory of the agent) and Project (memory of the specific project's working process) scopes. It supports **distilling** reusable content (behavioral patterns, problem-solving methodologies, workflow ideas) from the project memory to the global memory.
2. **Project Code Map (AST, Extended Feature)** — Builds a structural code map of the project, updated **incrementally** via commit diffs, eliminating the need to rescale the whole repository.
3. **Automated Memory Maintenance** — Captured automatically via sessions and hooks, regulated by an **ingestion gate** (deciding what is worth remembering and what is not), and **fed back** into subsequent tasks (strengthening recalled items and decaying/archiving unused ones).

v0.1 lacked the five primitives required to support these goals. This revision introduces them:

| Primitive | Objective |
|---|---|
| Scope Partitioning (Global Store / Project Store) | Goal 1 |
| Memory Classification (`fact` / `episode` / `procedure` / `preference`) | Goals 1 & 3 |
| Provenance (Source kinds + file/commit/session references) | Goals 2 & 3 |
| Retraction / Source-based Invalidation APIs | Goal 2 |
| Usage Feedback Metadata (`use_count` / `last_used_at` / `confidence`) | Goal 3 |

Additionally, we introduce a key architectural decision: **Separation of Capture and Digest** (§7), resolving the performance conflict between quick hooks (<50ms) and slow LLM extractions, all while maintaining a daemonless environment.

---

## 1. Project Positioning (Inherited from 0.1, Revised)

Membox is a **localized hierarchical memory system** providing unified memory services for coding agents (Claude Code, Cursor, Cline, Aider, etc.). Knowledge Graphs and RAG serve as its retrieval mechanisms, not its entire definition.

Core Propositions (Unchanged):

- **Hands-on Implementation** — No reliance on external services like Neo4j, Weaviate, or Pinecone. All logic is written from scratch in Python + SQLite.
- **CLI-First** — Delivered as a command-line tool. Agents integrate via skill files and hooks without requiring MCP or HTTP servers.
- **Zero External Services, Zero Daemons** — File-level SQLite storage.
- **Agent Sharing** — Multiple agents share memory through the same SQLite database file (via WAL and cross-process safety).

New Propositions:

- **Memory Lifecycle** — Ingestion is gated, usage is reinforced, and inactive memories decay and archive over time. Memory is not a run-only, append-only log.
- **Traceable and Retractable** — Every memory knows its source (session, file, commit, distillation). Derived facts can be invalidated if their source changes.

---

## 2. Core Concepts

### 2.1 Two-level Scopes

| Scope | Storage Location | Content | Lifecycle |
|---|---|---|---|
| **global** | `~/.membox/global.db` | Agent's general memory: user preferences, coding habits, common workflows. | Persists with the user (long-term). |
| **project** | `<project>/.membox/memory.db` | This project's operational memory: architectural facts, decisions, lessons, active session contexts. | Tied to the repository; deleted when the project is deleted. |

Design Decision: **Two independent DB files, rather than a single database with a scope column.** Rationale:

- Project memory stays with the repository (teams can choose to commit `.membox/` or gitignore it), while global memory is never leaked into the repository.
- Lifecycles are isolated, avoiding complex cascading cleanups across scopes.
- Aligns with existing practices (such as Claude Code's project CLAUDE.md vs. global configuration).
- Reuses v0.1 `KnowledgeStore` (one instance per file) with minimal changes.

**Project Store Resolution**: Search upwards from the current working directory (CWD) to locate the nearest `.membox/memory.db` (similar to `.git` discovery). Passing `--db` explicitly bypasses discovery (maintaining compatibility with v0.1). `membox init` initializes `.membox/` in the current directory.

**Retrieval Consolidation**: Defaults to `--scope all` — queries both stores concurrently, labeling the source scope of each result. On conflict, the project scope takes precedence (as it is more specific). Writes must target a single scope (defaulting to `project`; `distill` is the only mechanism that writes across scopes automatically).

### 2.2 Memory Classification

| Type | Definition | Typical Source | Primary Target |
|---|---|---|---|
| `fact` | Objective statements: "X project uses Neo4j." | Document ingestion, codemap | Triple index in knowledge graph |
| `episode` | Events: debugging sessions, decisions and their context. | Captured via session hooks | Project store; raw material for distillation |
| `procedure` | Workflows / Rules: "Run X before Y", coding patterns. | Distillation, manual entry | Primarily Global store |
| `preference` | Coding habits: "User prefers rebase over merge." | Distillation, manual entry | Primarily Global store |

Data Model Implication: **The `memories` table stores the ground-truth memory; the entity/relation graph is downgraded to a retrieval index.** `fact` type memories are still extracted into triples (matching v0.1 behavior); `episode`, `procedure`, and `preference` memories exist as independent memory units, optionally linked to a few index entities (to facilitate BFS recall) but are not forced into triples to prevent losing narrative or procedural context.

### 2.3 Provenance

Every memory must carry:

- `source_kind` — `manual` | `hook` | `file` | `commit` | `distill` | `ci`
- `source_ref` — Corresponding reference: file path, commit hash, session ID, or source memory IDs (for distillation)
- `created_at` / `updated_at`

Value: ① Incremental Updates — "File X changed → retract all derived facts where `source_ref = X` and reconstruct"; ② Audit Trails — Procedures in the global store can trace back to the project episodes they were distilled from; ③ Trust Grading — Manually entered memory starts with a higher confidence score than automatically hooked ones.

### 2.4 Lifecycle and Feedback

```
captured(inbox) → gated(gate check) → active → (reinforced ←→ decayed) → archived → purgeable
                         ↘ rejected (skip write, log audit count)      ↗
                                    retracted (source invalid / manually revoked)
```

- **Reinforcement**:`recall` / `query` hits increment `use_count` and refresh `last_used_at`.
- **Decay**: Triggered during `consolidate` — `confidence` decays based on elapsed time since `last_used_at`. Once it drops below a threshold (default 0.3), it transitions to `archived` (excluded from default retrieval, but queryable and restorable).
- **Retraction**: Retracted records remain in the DB (for auditing) but are excluded from default retrieval and graph indices.

---

## 3. Data Model

### 3.1 Migration Strategy

- Schema versioning is managed via `PRAGMA user_version`. `KnowledgeStore` automatically executes forward migrations (0 → 2) upon initialization.
- v0.1 → v0.2: The `documents` table is preserved and upgraded to `memories` via `ALTER TABLE ... RENAME` with additional columns (existing rows default to `type='fact', status='active', source_kind='manual'`). The schema for `entities`, `relations`, and `entity_aliases` remains unchanged (`relation_evidence.doc_id` now references `memories.id`).
- Migrations run in a transaction and rollback on failure. A backup database named `<db>.bak` is automatically created before migration.

### 3.2 Schema DDL (Identical schema for each scope database)

```sql
-- Memory Units (Upgraded from 0.1 documents table; source of truth)
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT    NOT NULL DEFAULT 'fact',
                  -- fact | episode | procedure | preference
    content       TEXT    NOT NULL,
    summary       TEXT    NOT NULL DEFAULT '',     -- One-line summary for preview & deduplication
    source        TEXT    NOT NULL DEFAULT '',     -- Retained for v0.1 compatibility
    source_kind   TEXT    NOT NULL DEFAULT 'manual',
                  -- manual | hook | file | commit | distill | ci
    source_ref    TEXT    NOT NULL DEFAULT '',     -- File path / commit SHA / session ID / 'mem:1,2,3'
    status        TEXT    NOT NULL DEFAULT 'active',
                  -- active | archived | retracted
    confidence    REAL    NOT NULL DEFAULT 1.0,
    use_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at  TEXT,
    embedding     BLOB,                            -- Summary embedding vector for semantic search
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT
);
CREATE INDEX idx_mem_status      ON memories(status);
CREATE INDEX idx_mem_source_ref  ON memories(source_kind, source_ref);
CREATE INDEX idx_mem_type        ON memories(type);

-- Raw Capture Inbox (Hooks write here; processed asynchronously)
CREATE TABLE inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT    NOT NULL,                 -- Raw observation text
    kind         TEXT    NOT NULL DEFAULT '',      -- Custom tags: tool_result / user_msg / outcome ...
    session_id   TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    digested_at  TEXT                              -- NULL = pending processing
);
CREATE INDEX idx_inbox_pending ON inbox(digested_at) WHERE digested_at IS NULL;

-- Entities / Aliases / Relations / Evidence (Same as v0.1, evidence references memories)
-- (entities, entity_aliases, and relations schema omit for brevity as they match v0.1)
CREATE TABLE relation_evidence (
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    memory_id   INTEGER NOT NULL REFERENCES memories(id)  ON DELETE CASCADE,
    PRIMARY KEY (relation_id, memory_id)
);
```

### 3.3 Code Map DB (Extension, stored in a separate file)

The codebase AST map is a **recomputable cache** that has a different lifecycle than empirical memory. Stored in a separate file at `<project>/.membox/codemap.db` to allow simple cache invalidation (e.g., complete rebuild on file change) without affecting the memory layer.

```sql
-- Graph schema mimics entities/relations (predicates: defines / has_method / calls / imports)
-- Incremental ledger:
CREATE TABLE code_files (
    path          TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,          -- File hash to skip unmodified files
    last_commit   TEXT NOT NULL DEFAULT '',
    indexed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE codemap_meta (
    key   TEXT PRIMARY KEY,               -- 'last_indexed_commit', etc.
    value TEXT NOT NULL
);
```

Incremental Update Protocol (`membox codemap update`):

1. Read `codemap_meta.last_indexed_commit`; if missing, fall back to full `build`.
2. Run `git diff --name-status <last>..HEAD` to get changed files (supports A/M/D/R).
3. For each deleted/modified/renamed-old path: retract all triples where `source_ref = <path>` in `codemap.db`.
4. For each added/modified/renamed-new path: parse with tree-sitter, insert nodes, and update `code_files`.
5. Update `last_indexed_commit`. The `memory.db` file is never touched.

Code map nodes can be merged into `recall` results (labeled as `source: codemap`) to provide an integrated context containing "module structure + relevant history decisions" to the agent.

---

## 4. Architectural Design

### 4.1 Module Layout (Incremental to v0.1)

```
src/membox/
├── model/
│   └── schema.py    # Added Memory, Observation, GateDecision, RecallResult models
├── core/
│   ├── store/       # Added memories/inbox CRUD, retract, reinforce, decay (new migrations in store/migrations.py)
│   ├── normalize.py # Unchanged
│   ├── lifecycle.py # New: Consolidate orchestration (digesting, decaying, archiving)
│   ├── scopes.py    # New: Scope resolution (global path, project detection, db handles)
│   ├── codemap.py   # New: codemap build/update orchestration (via git diff)
│   └── agent.py     # MemoryAgent extension: observe/recall/consolidate/distill entry points
├── services/
│   ├── extraction.py # Unchanged (Protocol + Dummy + OpenAI)
│   ├── embedding.py  # Unchanged
│   ├── gate.py      # New: Ingestion Gate (SalienceGate Protocol + LLM/Heuristic)
│   ├── distill.py   # New: Project-to-Global memory distillation
│   └── ast_parser.py # Tree-sitter parser (from Phase 8)
└── cli/             # Typer command registration (presentation-only)
```

Design principles remain unchanged: storage, LLM calls, orchestration, and CLI are strictly decoupled. `gate` and `distill` use dependency-injected protocols.

### 4.2 Key Design Decisions (New in v0.2)

| Decision | Selection | Rationale |
|---|---|---|
| Scope Isolation | Separate DB files | Lifecycle isolation, repo-based sharing, zero leaks across scopes (§2.1). |
| Ground Truth | `memories` table; graph is index | Avoids breaking narrative/procedural memory into rigid triples. |
| Hook Latency | Separation of Capture and Digest (inbox) | Hook execution path is zero-LLM, completing in <50ms (§7). |
| Digest Triggering | Opportunistic: SessionStart / explicit consolidate | Retains the daemonless, zero-background-process constraint. |
| Ingestion Gate | `SalienceGate` Protocol | Decoupled implementation (LLM or heuristic); mockable in tests. |
| Retraction Granularity | By `(source_kind, source_ref)` or by ID | Supports incremental diff updates and manual error corrections. |
| Code Map Isolation | Independent `codemap.db` | Recomputable caches do not pollute the core memory database. |
| Schema Evolution | `PRAGMA user_version` + auto-migration | Transparent upgrade for users with safe rollback capability. |

---

## 5. Interface Design

### 5.1 CLI Commands (All v0.1 commands are preserved)

```bash
# ---- Scope & Initialization ----
membox init                                  # Create .membox/ in the current directory
# All commands accept --scope {project|global|all}
# Write commands default to 'project', read commands default to 'all'
# --db path remains available (compatibility with v0.1, bypasses scope discovery)

# ---- Capture (Hook execution: fast & cheap, no LLM) ----
membox observe "<raw observation text>" --kind outcome --session <id>

# ---- Digest (Opportunistic or Manual execution) ----
membox consolidate [--scope project] [--dry-run]
#   1) Read pending batch from inbox → extract memory candidates via LLM
#   2) Run SalienceGate for each: save / merge / skip
#   3) For 'fact' types, extract triples into graph index
#   4) Run decay & archiving routines
#   --dry-run prints decisions without committing to database

# ---- Retrieval (With Feedback) ----
membox recall "<current task context>" [--scope all] [--types fact,procedure] [--budget 2000]
#   Retrieves from both databases (alias -> embedding -> BFS matching v0.1) + memory semantic search.
#   Ranks by confidence × relevance, fitting output to token budget.
#   Matching items get use_count++ and refreshed last_used_at.
#   (v0.1's 'membox query' is preserved for pure triple search and routes internally through recall)

# ---- Distillation ----
membox distill [--dry-run] [--min-episodes 3]
#   Scan project-level episodes/procedures → extract generalized procedures/preferences via LLM
#   → Write to global scope with source_kind=distill, source_ref=list of source IDs
#   --dry-run displays candidates; defaults to requiring confirmation before writing

# ---- Retraction & Cleanup ----
membox retract --id <N> | --source <ref> [--kind file]   # Mark retracted, remove from graph indices
membox forget --archived --older-than 90d                 # Physically delete archived records (optional)

# ---- Code Map (Extension) ----
membox codemap build <path> [--language python]
membox codemap update [--since <sha>]        # Default starts from last_indexed_commit
membox codemap query "<query>"               # Results are merged into recall --scope all
```

### 5.2 Python API (Incremental)

```python
agent = MemoryAgent(extractor=..., embedder=..., gate=...,   # gate is optional, defaults to heuristic
                    scope="auto")  # 'auto' resolves project + global paths; or explicit db_path (v0.1 compatibility)

agent.observe(text, kind="", session_id="") -> int            # Appends to inbox
agent.consolidate(dry_run=False) -> ConsolidateReport         # Digest + gate check + decay
agent.recall(context, scope="all", types=None, budget=2000) -> RecallResult
agent.distill(dry_run=True) -> list[DistillCandidate]
agent.retract(memory_id=None, source_ref=None) -> int
# v0.1 ingest/query/list_* APIs remain unchanged with original semantics
```

### 5.3 SalienceGate Protocol

```python
class SalienceGate(Protocol):
    def judge(self, candidate: Memory, similar: list[Memory]) -> GateDecision: ...
    # GateDecision: action ∈ {save, merge, skip} + target_id (on merge) + reason
```

Gate Criteria (LLM prompt guidelines; heuristic implementation checks a computable subset):

1. **Novelty** — Semantically identical to existing memory (checked via top-k embedding) → merge or skip.
2. **Reusability** — One-off details (temporary file paths, local compile errors) → skip; generalized lessons → save.
3. **Timeliness** — Transient states ("running CI tests") → skip.
4. **Actionability** — Preferences or procedures must contain instructions on "what to do next time" to be saved.
5. **Conservative Default** — When in doubt → skip (prefer missing a memory over polluting the database with noise).

---

## 6. Hook Integration (Automation Story for Goal 3)

v0.1 relied solely on "skill files teaching the agent to call the CLI" — this depends on the agent remembering to call it, which is exactly what a memory system should automate. v0.2 relies on hooks:

| Trigger Point | Action | Latency Budget |
|---|---|---|
| SessionStart | Run `membox recall "<project+branch+recent tasks>" --budget 2000` to inject context; followed by opportunistic `membox consolidate` (if inbox backlog exceeds threshold) | recall <200ms; consolidate runs as a background task |
| Tool Finished | `membox observe "<action outcomes/lessons>" --kind outcome` | <50ms (SQLite write append) |
| SessionEnd | `membox observe --kind session_summary` | <50ms |
| PostToolUse (Optional) | Observe outputs of high-signal tools | <50ms |

Key Rule: **The hook execution path contains zero LLM calls** — `observe` is a pure SQLite write. `consolidate` does use an LLM, but is scheduled at SessionStart (where users expect startup initialization delay) and can be disabled via configuration. Skill files (Phase 9) remain active to teach agents to run manual `recall` or save key conclusions via `observe`.

**Feedback Loop**: recall injects context → memory is utilized → `use_count`/`last_used_at` increment → memory ranking rises. Unrecalled items decay → archived. Rejected count from the gate is logged for auditing.

---

## 7. Quality Requirements (New in v0.2)

Additional test scenarios (zero external APIs, using mocked gate/extractor/embedder):

- **Scope Isolation** — Writing to project store does not affect global store; `--scope all` merges and deduplicates correctly; project path discovery (searching upwards).
- **Gating Check** — Test save/merge/skip paths; merging merges source evidence; conservative default path.
- **Lifecycle** — Reinforcement counts; archiving at decay threshold; archived items excluded from default recall; retraction invalidates graph index.
- **Retraction** — Batch retraction via `source_ref`; relations with zero remaining evidence are excluded from queries.
- **Migration** — Automigration of v0.1 database to v0.2; zero data loss; `.bak` creation.
- **Inbox** — Concurrent observes (multi-process writes); idempotent consolidation.
- **Incremental Code Map** — Modifying one file + committing → update only re-indexes that file; deleting a file removes its nodes and relationships.
- **Cross-process Safety** — `find_or_create` does not leak IntegrityError under concurrent writes (fix carried with phases 1-7).

Coverage (≥ 80%), strict mypy, and full ruff checks remain unchanged.

---

## 8. Revised Roadmap (Will be merged into roadmap.md upon approval)

```
Phase 8'  Storage Evolution: memories/inbox tables + migration + retract/reinforce/decay + scope discovery  ← Foundation
Phase 9'  Capture & Digest: observe + consolidate + SalienceGate (heuristic + LLM implementations)
Phase 10' Retrieval & Feedback: recall (dual DB consolidation, budget, reinforcement) + decay-archiving loop
Phase 11' Distillation: distill (project episode → global procedure/preference, with approval confirmation)
Phase 12' Hook Integration & Skill Files (Phase 9 extension: hook config examples + documentation)
Phase 13' Code Map: ast_parser + codemap build/update (incremental-first design)
Phase 14' Release Polish (Phase 10 original)
```

Scheduling Principle: **Schema primitives first** (adding columns in 8' is cheap; doing it after 13' would require a second migration); AST processing is deferred (as an extension, it depends on the provenance and retraction primitives introduced in 8').

---

## 9. Open Questions (Pending Decision)

1. **LLM Cost Control for Consolidate** — Should inbox batch digestion split by token budgets? Drop oldest raw observations if backlog exceeds limit? (Recommendation: Keep latest N observations + total token cap, drop oldest first on overflow).
2. **Should `.membox/` be gitignored by default?** — Team sharing (commit) vs. personal memory (ignore). (Recommendation: `membox init` writes to `.gitignore` by default, providing a `--shared` option to skip gitignoring).
3. **Global Store Concurrency** — Multiple projects writing to the global store concurrently. The v0.1 cross-process safety fix must be completed first (resolved in phases 1-7).
4. **Recall Raking Formula** — How should we weight `score = α · relevance + β · confidence + γ · recency`? (Recommendation: Start with 0.6 / 0.25 / 0.15, fine-tune later once gate rejection audit data accumulates).
5. **Code Map Languages Support Order** — Python first (for bootstrapping), then TypeScript/Go? (Based on actual project distribution).
