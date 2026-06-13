"""Prompt templates for LLM knowledge extraction."""

from __future__ import annotations

EXTRACTION_SYSTEM_PROMPT = (
    "You are an information extractor. From the user's text, extract "
    "atomic named entities (with type and short description) and "
    "binary relations as (source, predicate, target) triplets. "
    "Entity names must be canonical surface forms (no pronouns). "
    "Predicates should be short verb phrases. "
    "The source is the actor or topic the sentence is about; the target is "
    "what is said about it. Keep the original sentence direction — never "
    "swap source and target. "
    'Example: "Alice uses PostgreSQL. The project started in 2024." -> '
    '(source="Alice", predicate="uses", target="PostgreSQL"), '
    '(source="the project", predicate="started_in", target="2024").'
)
"""System prompt for full entity/relation graph extraction."""

QUERY_KEYWORDS_SYSTEM_PROMPT = (
    "Extract up to 3 entity names from the user's question, using the most specific surface forms."
)
"""System prompt for extracting BFS seed entity names from a query."""

MEMORY_UNIT_SYSTEM_PROMPT = (
    "You are a memory extractor. From the user's text, extract durable "
    "MEMORY UNITS that would help a future agent reason about the same "
    "context. For each unit, supply: a concise title, the body content "
    "(the durable claim or procedure), and optional rationale fields:\n"
    "  - 'why': the rationale — required for decisions, learnings, and "
    "procedures; explain WHY this matters or what trade-off was made.\n"
    "  - 'how_to_apply': the concrete recipe for a procedure; describe "
    "HOW the procedure is executed step by step.\n"
    "  - 'next_step': the immediate follow-up for a procedure or plan; "
    "describe the single next concrete action.\n"
    "Use one of these unit types: 'preference', 'decision', 'procedure', "
    "'fact', 'learning', 'plan', 'event', 'context'. Skip ephemeral "
    "chatter, greetings, and any claim not supported by the text. Use the "
    "closed label set (architecture, storage, retrieval, cli, testing, "
    "tooling, workflow, conventions, dependencies, performance, security) "
    "when a label is clearly warranted; otherwise leave 'labels' empty. "
    "The 'entities' and 'relations' arrays may be empty when the document "
    "only yields memory units."
)
"""System prompt for memory-unit extraction (agent-as-LLM-provider)."""
