"""membox — Local knowledge graph + RAG memory layer for coding agents."""

from __future__ import annotations

from membox.agent import MemoryAgent
from membox.embed import DummyEmbedder, Embedder
from membox.extract import DummyExtractor, LLMExtractor
from membox.schema import (
    Document,
    Entity,
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
    HopResult,
    Relation,
    Triple,
)
from membox.store import KnowledgeStore

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
    "Relation",
    "Triple",
    "__version__",
]
