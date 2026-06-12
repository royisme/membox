"""membox embedding service — domain-level Embedder Protocol and implementations.

The :class:`Embedder` Protocol is the domain interface used by the store and
agent (single text in, vector out). Implementations delegate HTTP to a
:class:`~membox.providers.base.EmbedClient` adapter from
:mod:`membox.providers`; the no-embedding degradation policy (string-only
entity deduplication) is expressed by passing ``embedder=None`` downstream.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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


@runtime_checkable
class BatchEmbedder(Protocol):
    """Optional extension for embedders that can batch and cache texts."""

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, preserving input order."""
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

    def __init__(
        self,
        client: EmbedClient,
        dim: int,
        *,
        model: str = "",
        cache_size: int = 10_000,
        batch_size: int = 128,
    ) -> None:
        self._client = client
        self.dim = dim
        self.model = model or getattr(client, "model", "")
        self.cache_size = max(cache_size, 0)
        self.batch_size = max(batch_size, 1)
        self._cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def embed(self, text: str) -> list[float]:
        """Embed a single text via the underlying provider client.

        Args:
            text: Text to embed.

        Returns:
            Float vector of length self.dim.
        """
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts via the underlying provider client.

        Cached values are returned without provider calls. Cache misses are
        de-duplicated and sent in batches while preserving the caller's input
        order in the returned list.

        Args:
            texts: Text strings to embed.

        Returns:
            One float vector per input text, in input order.
        """
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        missing: list[str] = []
        seen_missing: set[str] = set()

        for index, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                results[index] = cached
                continue
            if text not in seen_missing:
                seen_missing.add(text)
                missing.append(text)

        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            vectors = self._client.embed(batch)
            if len(vectors) != len(batch):
                msg = f"Embedding provider returned {len(vectors)} vectors for {len(batch)} texts"
                raise RuntimeError(msg)
            for text, vector in zip(batch, vectors, strict=True):
                self._cache_put(text, vector)

        for index, text in enumerate(texts):
            if results[index] is None:
                cached = self._cache_get(text)
                if cached is None:  # pragma: no cover - defensive
                    msg = f"Embedding cache was not populated for text: {text!r}"
                    raise RuntimeError(msg)
                results[index] = cached

        return [list(vector) for vector in results if vector is not None]

    def _cache_get(self, text: str) -> list[float] | None:
        """Return a cached vector and mark it recently used."""
        if self.cache_size <= 0:
            return None
        key = (self.model, text)
        vector = self._cache.get(key)
        if vector is None:
            return None
        self._cache.move_to_end(key)
        return list(vector)

    def _cache_put(self, text: str, vector: list[float]) -> None:
        """Store a vector and evict the least-recently-used entry at cap."""
        if self.cache_size <= 0:
            return
        key = (self.model, text)
        self._cache[key] = list(vector)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)


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
        *,
        cache_size: int = 10_000,
        batch_size: int = 128,
    ) -> None:
        from membox.providers.openai_compat import OpenAIEmbedClient

        super().__init__(
            OpenAIEmbedClient(client, model, dim),
            dim,
            model=model,
            cache_size=cache_size,
            batch_size=batch_size,
        )
        self.client = client
        self.model = model
