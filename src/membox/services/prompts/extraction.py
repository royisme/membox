"""Prompt templates for LLM knowledge extraction."""

from __future__ import annotations

EXTRACTION_SYSTEM_PROMPT = (
    "You are an information extractor. From the user's text, extract "
    "atomic named entities (with type and short description) and "
    "binary relations as (source, predicate, target) triplets. "
    "Entity names must be canonical surface forms (no pronouns). "
    "Predicates should be short verb phrases."
)
"""System prompt for full entity/relation graph extraction."""

QUERY_KEYWORDS_SYSTEM_PROMPT = (
    "Extract up to 3 entity names from the user's question, using the most specific surface forms."
)
"""System prompt for extracting BFS seed entity names from a query."""
