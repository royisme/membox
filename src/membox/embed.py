"""membox embed — embedding Protocol and implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openai import OpenAI


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


class OpenAIEmbedder:
    """Embedder backed by OpenAI's text-embedding API.

    Requires the ``openai`` package (``pip install membox[llm]``) and a valid
    ``OPENAI_API_KEY``.
    """

    def __init__(
        self,
        client: OpenAI,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
    ) -> None:
        self.client = client
        self.model = model
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        """Embed text using the OpenAI embeddings API.

        Args:
            text: Text to embed.

        Returns:
            Float vector of length self.dim.
        """
        rsp = self.client.embeddings.create(model=self.model, input=text, dimensions=self.dim)
        return list(rsp.data[0].embedding)
