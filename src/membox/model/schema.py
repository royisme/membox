"""membox schema — Pydantic data models for the knowledge graph."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedEntity(BaseModel):
    """An entity extracted from text by an LLM."""

    name: str = Field(description="Entity name as it appears in text")
    type: str = Field(description="Entity type, e.g. Person, Project, Technology")
    description: str = Field(default="", description="Short description")


class ExtractedRelation(BaseModel):
    """A binary relation extracted from text by an LLM."""

    source: str
    target: str
    predicate: str = Field(description="Short verb phrase, e.g. 'uses', 'developed'")


class ExtractedGraph(BaseModel):
    """Full extraction result: entities and relations from one document."""

    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]


class Entity(BaseModel):
    """A knowledge-graph entity stored in the database."""

    id: int
    name: str
    type: str
    embedding: list[float] | None = None
    created_at: str = ""


class Relation(BaseModel):
    """A directed relation between two entities."""

    id: int
    source_id: int
    target_id: int
    predicate: str
    source_name: str = ""
    target_name: str = ""


class Document(BaseModel):
    """A source document stored for evidence lineage."""

    id: int
    content: str
    source: str = ""
    created_at: str = ""


class Triple(BaseModel):
    """A normalized knowledge-graph triple."""

    source: str
    predicate: str
    target: str
    source_type: str = ""
    target_type: str = ""


class HopResult(BaseModel):
    """Result of a BFS multi-hop retrieval query."""

    triplets: list[tuple[str, str, str]] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    seed_names: list[str] = Field(default_factory=list)
    visited_entities: list[str] = Field(default_factory=list)
