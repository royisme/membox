"""membox extraction service — domain-level LLMExtractor Protocol and implementations.

The :class:`LLMExtractor` Protocol is the domain interface used by the agent
(text in, validated :class:`~membox.model.schema.ExtractedGraph` out).
Implementations compose a :class:`~membox.providers.base.ChatClient` adapter
with the prompt templates from :mod:`membox.services.prompts` and Pydantic
response validation; they never speak HTTP directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from membox.model.schema import ExtractedGraph
from membox.services.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    QUERY_KEYWORDS_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from openai import OpenAI

    from membox.providers.base import ChatClient
    from membox.services.embedding import Embedder


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


class _Keywords(BaseModel):
    """Structured-output schema for query seed extraction."""

    keywords: list[str]


class ExtractionService:
    """Domain extraction service over a low-level :class:`ChatClient`.

    Combines the extraction prompts with JSON-constrained completions and
    validates the responses into Pydantic models. Satisfies the
    :class:`LLMExtractor` Protocol.
    """

    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    def extract(self, text: str) -> ExtractedGraph:
        """Extract entities and relations via a JSON-constrained completion.

        Args:
            text: Document text to extract from.

        Returns:
            Validated ExtractedGraph; empty on unparseable model output.
        """
        raw = self._chat.complete(EXTRACTION_SYSTEM_PROMPT, text, json_schema=ExtractedGraph)
        try:
            return ExtractedGraph.model_validate_json(raw)
        except ValidationError:
            return ExtractedGraph(entities=[], relations=[])

    def extract_query_entities(self, query: str) -> list[str]:
        """Extract up to 3 entity seed names from a natural language query.

        Args:
            query: Natural language question.

        Returns:
            List of up to 3 entity name strings; empty on unparseable output.
        """
        raw = self._chat.complete(QUERY_KEYWORDS_SYSTEM_PROMPT, query, json_schema=_Keywords)
        try:
            return _Keywords.model_validate_json(raw).keywords
        except ValidationError:
            return []


class OpenAIExtractor(ExtractionService):
    """LLM extractor backed by an OpenAI-compatible structured-output API.

    Compatibility wrapper: builds the provider adapter from a raw ``openai``
    SDK client and keeps the historical ``client``/``model`` attributes.
    Requires the ``openai`` package (``pip install membox[llm]``) and a valid
    API key.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        from membox.providers.openai_compat import OpenAIChatClient

        super().__init__(OpenAIChatClient(client, model))
        self.client = client
        self.model = model


def create_default_extractor(
    use_llm: bool = True,
) -> tuple[LLMExtractor, Embedder | None]:
    """Select the best available extraction backend.

    Returns OpenAI-backed implementations when the configured extraction API
    key resolves (``OPENAI_API_KEY`` by default, see
    :class:`~membox.config.MemboxConfig`) and the ``openai`` package is
    importable; otherwise falls back to the no-op :class:`DummyExtractor`
    with no embedder. The ``openai`` import is lazy so the package remains an
    optional dependency.

    Args:
        use_llm: When False, skip backend detection and return the Dummy backend.

    Returns:
        Tuple of (extractor, embedder). The embedder is None for the Dummy backend.
    """
    from membox.config import MemboxConfig

    config = MemboxConfig()
    api_key = config.extraction.resolved_api_key()
    if use_llm and api_key:
        try:
            from openai import OpenAI
        except ImportError:
            return DummyExtractor(), None
        from membox.services.embedding import OpenAIEmbedder

        client = OpenAI(api_key=api_key, base_url=config.extraction.base_url)
        return (
            OpenAIExtractor(client, model=config.extraction.model),
            OpenAIEmbedder(
                client,
                model=config.embedding.model,
                dim=config.embedding.dimensions,
            ),
        )
    return DummyExtractor(), None
