"""membox extract — LLM extraction Protocol and implementations."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from membox.schema import ExtractedGraph

if TYPE_CHECKING:
    from openai import OpenAI

    from membox.embed import Embedder


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


class OpenAIExtractor:
    """Real LLM extractor backed by OpenAI structured outputs.

    Requires the ``openai`` package (``pip install membox[llm]``) and a valid
    ``OPENAI_API_KEY``.

    Uses ``client.beta.chat.completions.parse`` with ``ExtractedGraph`` as the
    response schema so the model output is already validated Pydantic objects.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.client = client
        self.model = model

    def extract(self, text: str) -> ExtractedGraph:
        """Extract entities and relations via OpenAI structured output.

        Args:
            text: Document text to extract from.

        Returns:
            Validated ExtractedGraph.
        """
        rsp = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an information extractor. From the user's text, extract "
                        "atomic named entities (with type and short description) and "
                        "binary relations as (source, predicate, target) triplets. "
                        "Entity names must be canonical surface forms (no pronouns). "
                        "Predicates should be short verb phrases."
                    ),
                },
                {"role": "user", "content": text},
            ],
            response_format=ExtractedGraph,
        )
        parsed = rsp.choices[0].message.parsed
        if parsed is None:
            return ExtractedGraph(entities=[], relations=[])
        return parsed

    def extract_query_entities(self, query: str) -> list[str]:
        """Extract up to 3 entity seed names from a natural language query.

        Args:
            query: Natural language question.

        Returns:
            List of up to 3 entity name strings.
        """
        from pydantic import BaseModel

        class _Keywords(BaseModel):
            keywords: list[str]

        rsp = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract up to 3 entity names from the user's question, "
                        "using the most specific surface forms."
                    ),
                },
                {"role": "user", "content": query},
            ],
            response_format=_Keywords,
        )
        parsed = rsp.choices[0].message.parsed
        if parsed is None:
            return []
        return parsed.keywords


def create_default_extractor(use_llm: bool = True) -> tuple[LLMExtractor, Embedder | None]:
    """Select the best available extraction backend.

    Returns OpenAI-backed implementations when ``OPENAI_API_KEY`` is set and the
    ``openai`` package is importable; otherwise falls back to the no-op
    :class:`DummyExtractor` with no embedder. The ``openai`` import is lazy so
    the package remains an optional dependency.

    Args:
        use_llm: When False, skip backend detection and return the Dummy backend.

    Returns:
        Tuple of (extractor, embedder). The embedder is None for the Dummy backend.
    """
    if use_llm and os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI
        except ImportError:
            return DummyExtractor(), None
        from membox.embed import OpenAIEmbedder

        client = OpenAI()
        return OpenAIExtractor(client), OpenAIEmbedder(client)
    return DummyExtractor(), None
