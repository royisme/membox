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
