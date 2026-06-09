"""membox embed — embedding Protocol and stub implementation."""

from __future__ import annotations

from typing import Protocol


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
