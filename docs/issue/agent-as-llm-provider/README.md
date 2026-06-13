<!-- Design spec for agent-as-LLM-provider: the calling coding agent supplies extraction, removing the LLM API as a hard dependency. -->

# Agent-as-LLM-provider — the calling agent supplies extraction

**Status**: design accepted (2026-06-12) — pending implementation
**Owner decisions locked**: agent-as-provider is the **primary interactive
mode** (not just a fallback); CLI protocol is **two commands**
(`extract-prompt` → `ingest-graph`).

## Problem

Membox has two external service dependencies — an **LLM** (chat/extraction) and
an **embedder**. The question: when the user configures *neither*, what happens,
and should the calling coding agent (Claude Code) act as membox's LLM provider?

### What actually needs what (verified)

| Capability | Needs | With nothing configured |
|---|---|---|
| **Recall** (`query`, `--include-memory`) | nothing | ✅ works — deterministic BFS + BM25/FTS + greedy knapsack; no summarization LLM (`core/agent.py` `compact_query`) |
| **Checkpoint capture** (triage → extract) | nothing | ✅ works — deterministic heuristic gate |
| `memory consolidate` (dream) / `distill` | nothing | ✅ works — deterministic; LLM comparator is opt-in, never constructed |
| **Vector recall** (quality boost) | embedder | ⚠️ degrades to FTS-only (CJK trigram + BM25) |
| **Semantic KG extraction** (`ingest` / `ingest-file`) | **LLM** | ❌ silently falls back to `DummyExtractor` → stores **0 entities** |

So the recall and capture paths — the agent's most important operations — need
**neither** dependency today. The *only* hole is document/text → KG semantic
extraction. That hole is where agent-as-provider fits.

## Decision — split the two dependencies

### LLM → delegate to the calling agent (primary interactive mode)

The agent invoking membox **is** a strong LLM. In interactive use it should do
the extraction itself; a separately-configured LLM API is reserved for
**agent-absent** contexts (cron / CI / batch ingest / v0.2 headless hooks).

This is self-consistent with membox's identity (CLI-first, no external services,
skill-driven) and removes the last external dependency from the interactive
path: membox becomes self-contained = SQLite + the agent already in the room.
It also means manual real-machine testing needs **no API key**.

The seams already exist — only a CLI surface is missing:

- `LLMExtractor` Protocol — `services/extraction.py:25-51`: `extract(text) ->
  ExtractedGraph`, `extract_query_entities(query) -> list[str]`.
- `MemoryAgent.ingest_extracted(text, graph, source="", *, project, source_path,
  section, doc_date) -> dict[str, int]` — `core/agent.py:77-100`, bypasses the
  LLM entirely.
- `ExtractedGraph` — `model/schema.py:300-308`: `entities: list[ExtractedEntity]`
  + `relations: list[ExtractedRelation{source, target, predicate}]`,
  Pydantic/JSON-serialisable.

### Embeddings → keep optional, degrade to FTS; never delegate to the agent

The agent is not an embedding model; producing vectors via tool calls is
impractical and would pollute the protocol. Embeddings stay **optional**: with
no embedder, retrieval already falls back to alias-exact + FTS5 BM25, and the
CJK trigram sidecar handles Chinese better than generic vectors anyway. Users
who want vector recall **opt in** to a local embedder (e.g. Ollama embedding
model). No default-on Ollama.

## CLI protocol — two commands

`membox` owns the prompt; the agent owns inference; `membox` owns
storage + validation.

```
1. membox extract-prompt <file|->  [--for entities|query]
     → prints the canonical extraction prompt + JSON schema (sourced from
       services/prompts/), wrapping the file's content. No LLM call.

2. (agent reads the file, runs the prompt, produces ExtractedGraph JSON)

3. membox ingest-graph --from-json -  --source <path> [--section ... --doc-date ...]
     → reads ExtractedGraph JSON from stdin, validates (Pydantic), calls
       MemoryAgent.ingest_extracted(...). Reports entities/relations stored.
```

Keeping the prompt in `services/prompts/` (module constants) means it is a
single source of truth — the skill only wires the steps together, it does not
duplicate the prompt/schema.

`--for query` reuses the same seam for `extract_query_entities` if we later want
agent-supplied query seeds; v1 may ship only `--for entities`.

## Footgun fix (do regardless of the above)

Today `ingest`/`ingest-file` with no configured LLM silently uses
`DummyExtractor` and stores **0 entities** — looks like success, builds nothing
(the same silent-loss anti-pattern as S1-D4). After this change, a no-extractor
ingest must **announce** it: e.g. `no extractor configured — entities not
extracted; configure an LLM or use the agent-as-provider flow
(membox extract-prompt → ingest-graph)`. Never a silent empty graph.

## Configuration tiers

| Tier | Config | Behavior |
|---|---|---|
| **Zero-config interactive** | nothing | recall + checkpoint work; KG extraction via agent-as-provider (`extract-prompt`→`ingest-graph`). No API key. |
| **+ vector recall** | opt-in embedder (e.g. Ollama) | adds vector disambiguation/recall on top |
| **Headless / batch** | configured LLM (+optional embedder) | agent-absent contexts: cron, CI, bulk ingest, v0.2 hooks call the LLM directly |

## Skill changes

Add to `skills/membox-skill.md` a "Remember a document into the knowledge graph"
workflow:

> To store a doc/handoff in the KG: `membox extract-prompt <file>` → produce the
> ExtractedGraph JSON it asks for → `membox ingest-graph --from-json - --source
> <file>`. (No LLM service needed — you are the extractor.)

Keep `ingest-file` documented for the configured-LLM path; agent-as-provider is
the default when no LLM is configured.

## Non-goals

- Do **not** delegate embeddings to the agent.
- Do **not** default-enable any local LLM/embedder (no implicit Ollama).
- Do **not** make `extract`/`triage` (checkpoint path) LLM-driven — they stay
  deterministic.
- No multi-chunk round-trip orchestration in v1: agent-as-provider targets small
  interactive payloads (handoffs, notes); bulk multi-chunk docs are the
  configured-LLM path's job.

## Implementation checklist

- [ ] `cli/commands/extract_prompt.py`: `extract-prompt <file|-> [--for entities|query]`; pull template from `services/prompts/`.
- [ ] `cli/commands/ingest_graph.py`: `ingest-graph --from-json - --source ...`; validate `ExtractedGraph`, call `ingest_extracted`; clear error on invalid JSON/schema (re-promptable).
- [ ] Register both on the root `app`.
- [ ] No-extractor `ingest`/`ingest-file`: replace silent `DummyExtractor` no-op with an explicit warning pointing to the agent-as-provider flow.
- [ ] `services/prompts/`: ensure the extraction prompt + JSON schema is a reusable constant/builder emitted by `extract-prompt`.
- [ ] Tests: `tests/test_agent_provider.py` — `extract-prompt` output shape; `ingest-graph` happy path (entities/relations stored == fed); invalid-JSON error; no-extractor ingest warns (not silent).
- [ ] Update `skills/membox-skill.md` with the extract-prompt → ingest-graph workflow.
- [ ] `scripts/update_repository_map.py` after adding files.
- [ ] Green gate: pytest + ruff + mypy.
