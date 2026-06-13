"""Service layer: domain-level LLM extraction and embedding capabilities.

Services own prompts, response parsing, and fallback policy. They delegate
all HTTP/wire-protocol concerns to :mod:`membox.providers`.
"""

from __future__ import annotations

from membox.services.embedding import DummyEmbedder, Embedder, EmbeddingService, OpenAIEmbedder
from membox.services.extraction import (
    ComparatorScore,
    DummyExtractor,
    ExtractionService,
    LLMComparator,
    LLMExtractor,
    OpenAIExtractor,
    create_default_extractor,
)

__all__ = [
    "ComparatorScore",
    "DummyEmbedder",
    "DummyExtractor",
    "Embedder",
    "EmbeddingService",
    "ExtractionService",
    "LLMComparator",
    "LLMExtractor",
    "OpenAIEmbedder",
    "OpenAIExtractor",
    "create_default_extractor",
]
