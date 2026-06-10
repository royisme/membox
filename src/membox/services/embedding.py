"""membox embedding service — domain-level Embedder Protocol and implementations.

The :class:`Embedder` Protocol is the domain interface used by the store and
agent (single text in, vector out). Implementations delegate HTTP to a
:class:`~membox.providers.base.EmbedClient` adapter from
:mod:`membox.providers`; the no-embedding degradation policy (string-only
entity deduplication) is expressed by passing ``embedder=None`` downstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openai import OpenAI

    from membox.providers.base import EmbedClient


class Embedder(Protocol):
    """Protocol for text-to-vector embedding."""

    dim: int

    def embed(self, text: str) -> list[float]:
        """Embed a text string into a fixed-dimensional float vector.

        Args:
            text: Input text to embed.

        Returns:
            Float vector of length self.dim.
        """
        ...


class DummyEmbedder:
    """Zero-vector embedder for tests that do not need embedding-based entity matching."""

    dim: int = 4

    def embed(self, text: str) -> list[float]:
        """Return a zero vector of length self.dim.

        Args:
            text: Ignored.

        Returns:
            List of dim zeros.
        """
        return [0.0] * self.dim


class EmbeddingService:
    """Domain embedding service over a low-level :class:`EmbedClient`.

    Satisfies the :class:`Embedder` Protocol while keeping all wire-protocol
    concerns inside the injected provider client.
    """

    def __init__(self, client: EmbedClient, dim: int) -> None:
        self._client = client
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        """Embed a single text via the underlying provider client.

        Args:
            text: Text to embed.

        Returns:
            Float vector of length self.dim.
        """
        return self._client.embed([text])[0]


class OpenAIEmbedder(EmbeddingService):
    """Embedder backed by an OpenAI-compatible embeddings API.

    Compatibility wrapper: builds the provider adapter from a raw ``openai``
    SDK client and keeps the historical ``client``/``model``/``dim``
    attributes. Requires the ``openai`` package (``pip install membox[llm]``)
    and a valid API key.
    """

    def __init__(
        self,
        client: OpenAI,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
    ) -> None:
        from membox.providers.openai_compat import OpenAIEmbedClient

        super().__init__(OpenAIEmbedClient(client, model, dim), dim)
        self.client = client
        self.model = model
