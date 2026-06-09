"""membox extract — LLM extraction Protocol and stub implementation."""

from __future__ import annotations

from typing import Protocol

from membox.schema import ExtractedGraph


class LLMExtractor(Protocol):
    """Protocol for LLM-backed triple extraction from natural language text."""

    def extract(self, text: str) -> ExtractedGraph:
        """Extract entities and relations from text.

        Args:
            text: Input document text.

        Returns:
            Extracted graph with entities and relations.
        """
        ...

    def extract_query_entities(self, query: str) -> list[str]:
        """Extract seed entity names from a query string.

        Args:
            query: Natural language query.

        Returns:
            List of entity name strings to use as BFS seeds.
        """
        ...


class DummyExtractor:
    """No-op extractor for tests. Use agent.ingest_extracted() to bypass LLM extraction."""

    def extract(self, text: str) -> ExtractedGraph:
        """Return an empty graph without calling any LLM.

        Args:
            text: Ignored.

        Returns:
            Empty ExtractedGraph.
        """
        return ExtractedGraph(entities=[], relations=[])

    def extract_query_entities(self, query: str) -> list[str]:
        """Return an empty seed list.

        Args:
            query: Ignored.

        Returns:
            Empty list.
        """
        return []
