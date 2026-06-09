# Agent Project Contract

Read this file when a task may affect product direction, dependencies, storage architecture, or agent-facing behavior.

## Product Constraints

Membox is a **local knowledge graph + RAG memory layer for coding agents**.

Non-negotiable defaults:

- **CLI-first**: `membox` CLI is the primary interface. Agents should use shell commands, not hidden in-process workflows.
- **Zero external service by default**: Core functionality must not require Neo4j, Weaviate, Pinecone, HTTP servers, MCP servers, daemons, or hosted services.
- **SQLite file storage**: Default persistence is a local SQLite database file shared by local agents/processes.
- **Hands-on implementation**: Prefer direct Python + SQLite implementation over heavy frameworks that hide core behavior.
- **Agent-shared memory**: Multiple coding agents should be able to share entities, relations, and evidence through the same database file.

Any design that deviates from these defaults must explicitly state the trade-off and get user approval before implementation.

## Source of Truth

When documents conflict, use this priority order:

1. `docs/spec.md` — product positioning, architecture, data model, and quality requirements.
2. `docs/roadmap.md` — implementation sequence.
3. `docs/code-standards.md` — style and engineering conventions.
4. `docs/workflow.md` — repository workflow.
5. `pyproject.toml` — actual toolchain, dependency versions, and lint/type/test config.

Do not silently change direction when implementation and docs disagree. Decide whether code is behind the spec or the spec is outdated, then mention that in the final response.

## Core Domain Invariants

### Knowledge Graph

- Core model is `(Entity) --[predicate]--> (Entity)`.
- `relations` must be unique by `(source_id, target_id, predicate)`.
- Evidence is many-to-many: one relation can reference multiple documents, and one document can support multiple relations.
- Retrieval responses should preserve path and evidence context so agents can cite why a fact was returned.

### Entity Disambiguation

Use the cascade defined in `docs/spec.md`:

1. Exact alias match.
2. Embedding cosine similarity `>= 0.85` among same-type entities.
3. Create a new entity.

If no embedder is configured, core behavior must still work through normalized string and alias matching.

### Predicate Normalization

- Normalize predicates before persistence.
- Apply lowercase normalization and explicit synonym mapping.
- Known examples: `developed`, `develop`, `开发` → `develops`.
- Prefer a small dictionary before fuzzy or LLM-based normalization.
