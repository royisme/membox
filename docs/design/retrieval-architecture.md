# Retrieval Architecture and Data Flow

Status: current after graph + FTS fusion
Date: 2026-06-11

This document explains the read-path architecture that powers `membox query`.
It focuses on the default budget-partitioned graph + FTS fusion path and the
fallback compatibility mode kept for A/B testing.

## Component Architecture

```mermaid
flowchart TD
    CLI["membox query"] --> Agent["MemoryAgent.compact_query"]
    Agent --> Extractor["LLMExtractor.extract_query_entities"]
    Agent --> Embedder["Embedder.embed(query), optional"]
    Agent --> Store["KnowledgeStore"]

    Store --> Entities["entities.py<br/>alias lookup and similar-entity lookup"]
    Store --> Retrieval["retrieval.py<br/>BFS, scoring, FTS, rendering"]
    Store --> Documents["documents table + documents_fts"]
    Store --> Relations["relations + relation_evidence"]

    Retrieval --> TriplePool["Triple pool<br/>BFS + composite score"]
    Retrieval --> ChunkPool["Chunk pool<br/>FTS5 BM25 chunks"]
    TriplePool --> FusedRenderer["fused_output"]
    ChunkPool --> FusedRenderer
    FusedRenderer --> Output["Compact context<br/>triples, source chunks, footer"]
```

The extractor may use a chat model to identify seed entities. Fusion, scoring,
budgeting, and rendering do not add any LLM calls. If no extractor or embedder
is available, retrieval still works through aliases, exact names, graph edges,
and FTS chunks.

## Default Query Data Flow

```mermaid
sequenceDiagram
    participant User as Agent or developer
    participant CLI as membox CLI
    participant MA as MemoryAgent
    participant KS as KnowledgeStore
    participant DB as SQLite + FTS5

    User->>CLI: membox query "question" --budget 2000
    CLI->>MA: compact_query(question)
    MA->>MA: extract query seed names
    MA->>KS: resolve seeds by alias or embedding
    MA->>KS: scored_query(seed_ids, question)
    KS->>DB: BFS over relations and evidence
    KS->>DB: FTS5 BM25 over relation evidence
    DB-->>KS: scored triple pool
    MA->>KS: fts_fallback_chunks(question, limit=5)
    KS->>DB: OR-of-tokens FTS5 search over documents
    DB-->>KS: ranked chunk pool
    MA->>KS: fused_output(scored, chunks, budget, chunk_share)
    KS-->>MA: compact fused context
    MA-->>CLI: append pending-ingest note if needed
    CLI-->>User: print context
```

## Fusion Renderer

```mermaid
flowchart TD
    Start["Inputs<br/>scored triples, FTS chunks, budget"] --> Partition["chunk_reserve = floor(budget * chunk_share)<br/>triple_allowance = budget - chunk_reserve"]

    Partition --> Pass1["Pass 1: triple pool<br/>admit triple lines and top-K evidence"]
    Pass1 --> P1Left["Pass 1 leftover"]

    P1Left --> Pass2["Pass 2: chunk pool<br/>budget = chunk_reserve + leftover<br/>skip chunks whose doc_id was emitted as evidence"]
    Pass2 --> P2Left["Pass 2 leftover"]

    P2Left --> Pass3["Pass 3: triple backfill<br/>admit remaining triple lines only"]
    Pass3 --> Render["Render output"]

    Render --> TripleSection["Subject-grouped triples"]
    Render --> EvidenceSection["Graph evidence snippets"]
    Render --> ChunkSection["Relevant source chunks"]
    Render --> Footer["Coverage footer<br/>N/M triples, K/L FTS chunks, X/Y tokens"]
```

The renderer uses skip-and-continue admission. Oversized items are skipped
instead of stopping the pass, so later cheaper items can still fit.

## Storage Surfaces Used by Retrieval

```mermaid
erDiagram
    entities ||--o{ entity_aliases : has
    entities ||--o{ relations : source
    entities ||--o{ relations : target
    relations ||--o{ relation_evidence : supported_by
    documents ||--o{ relation_evidence : supports
    documents ||--|| documents_fts : indexed_by

    entities {
        integer id
        text name
        text type
        blob embedding
    }

    relations {
        integer id
        integer source_id
        integer target_id
        text predicate
        blob embedding
        integer superseded_by
    }

    documents {
        integer id
        text content
        text project
        text source_path
        text section
        text doc_date
        integer version
    }

    documents_fts {
        text content
    }
```

## Control Modes

```mermaid
flowchart LR
    Config["RetrievalConfig.fusion_mode"] --> Merge["merge (default)"]
    Config --> Fallback["fallback"]

    Merge --> M1["Always fetch triple pool when seeds resolve"]
    Merge --> M2["Always fetch chunk pool when fts_fallback_k > 0"]
    Merge --> M3["Render through fused_output"]

    Fallback --> F1["No seeds or no triples"]
    Fallback --> F2["Direct FTS fallback output"]
    Fallback --> F3["Graph hit"]
    Fallback --> F4["Graph-only compact_output"]
```

Use `fusion_mode="fallback"` only for A/B comparison, regression diagnosis, and
rollback. The default product behavior is `fusion_mode="merge"`.

## Acceptance Snapshot

The shipped Gemini defaults (`fusion_mode="merge"`, `chunk_share=0.4`,
`fts_fallback_k=10`) reached:

- Overall: 23/26, 88.5%.
- Single-hop: 13/15, 86.7%.
- Multi-hop: 6/7, 85.7%.
- Temporal: 4/4, 100%.
- Mean output: 1941 estimated tokens.

The acceptance threshold was overall >= 80%, temporal 100%, multi-hop at least
4/7, and default 2000-token budget.
