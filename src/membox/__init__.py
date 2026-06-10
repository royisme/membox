"""membox — Local knowledge graph + RAG memory layer for coding agents."""

from __future__ import annotations

from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.model.schema import (
    Document,
    Entity,
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
    HopResult,
    Relation,
    Triple,
)
from membox.services.embedding import DummyEmbedder, Embedder, OpenAIEmbedder
from membox.services.extraction import DummyExtractor, LLMExtractor, OpenAIExtractor

__version__ = "0.1.0"

__all__ = [
    "Document",
    "DummyEmbedder",
    "DummyExtractor",
    "Embedder",
    "Entity",
    "ExtractedEntity",
    "ExtractedGraph",
    "ExtractedRelation",
    "HopResult",
    "KnowledgeStore",
    "LLMExtractor",
    "MemoryAgent",
    "OpenAIEmbedder",
    "OpenAIExtractor",
    "Relation",
    "Triple",
    "__version__",
]
