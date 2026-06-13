"""membox schema — Pydantic data models for the knowledge graph and history trace."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class SourceKind(StrEnum):
    """Origin of an imported history trace record.

    ``source_kind`` is carried per record and prefixes all trace IDs so
    sessions imported from different tools can never collide.  Values are
    stored as plain text (no SQL CHECK constraint) so the enum can evolve
    without a table rebuild.
    """

    CODEX_JSONL = "codex-jsonl"
    CLAUDE_JSONL = "claude-jsonl"
    MIMO_SQLITE = "mimo-sqlite"
    MEMBOX_CAPTURE = "membox-capture"
    PI_JSONL = "pi-jsonl"
    MANUAL = "manual"


class HistoryEventKind(StrEnum):
    """Kind of a non-message history event.

    ``TOOL_ERROR`` is not stored directly: failed tool results are stored as
    ``TOOL_RESULT`` with ``is_error=1`` and ``tool_error`` is accepted as a
    search-filter alias for that combination.
    """

    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    OTHER = "other"


class MemoryUnitType(StrEnum):
    """Closed Phase C memory unit taxonomy."""

    PREFERENCE = "preference"
    DECISION = "decision"
    PROCEDURE = "procedure"
    FACT = "fact"
    LEARNING = "learning"
    PLAN = "plan"
    EVENT = "event"
    CONTEXT = "context"


class MemoryUnitStatus(StrEnum):
    """Lifecycle status values stored on ``memory_units``."""

    UNIT_CANDIDATE = "unit_candidate"
    ACTIVE_UNIT = "active_unit"
    CRYSTAL_CANDIDATE = "crystal_candidate"
    CRYSTAL = "crystal"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    RETRACTED = "retracted"


class MemoryTemporalType(StrEnum):
    """Temporal semantics for a triage decision or memory unit."""

    POINT = "point"
    RANGE = "range"
    ONGOING = "ongoing"
    UNKNOWN = "unknown"


class MemoryUserIntent(StrEnum):
    """Whether capture was user-initiated or automatic."""

    MANUAL = "manual"
    AUTO = "auto"


class MemorySourceKind(StrEnum):
    """Valid source kinds for ``memory_unit_sources``."""

    HISTORY_MESSAGE = "history_message"
    HISTORY_EVENT = "history_event"
    DOCUMENT = "document"
    RELATION = "relation"
    UNIT = "unit"
    MANUAL = "manual"


class TraceKind(StrEnum):
    """Trace row kinds consumed by the Phase C triage table."""

    MESSAGE = "message"
    EVENT = "event"


MEMORY_LABELS: frozenset[str] = frozenset(
    {
        "architecture",
        "storage",
        "retrieval",
        "cli",
        "testing",
        "tooling",
        "workflow",
        "conventions",
        "dependencies",
        "performance",
        "security",
    }
)
"""Closed Phase C memory label set."""


class HistoryTriageRecord(BaseModel):
    """A persisted deterministic triage decision for one trace item."""

    id: int | None = None
    project: str = ""
    trace_kind: TraceKind
    trace_id: str
    should_extract: bool
    unit_type: MemoryUnitType
    importance_score: float = 0.0
    confidence_score: float = 0.0
    temporal_type: MemoryTemporalType = MemoryTemporalType.UNKNOWN
    user_intent: MemoryUserIntent = MemoryUserIntent.AUTO
    extraction_hint: str = ""
    reason: str = ""
    gate_version: str
    consumed_at: str | None = None
    created_at: str = ""


class MemoryUnitSource(BaseModel):
    """One source reference attached to a memory unit."""

    source_kind: MemorySourceKind
    source_ref: str
    source_message_id: str = ""
    quote: str = ""


class MemoryUnitRecord(BaseModel):
    """A Phase C memory unit row plus labels and source references."""

    id: int | None = None
    project: str = ""
    unit_type: MemoryUnitType
    status: MemoryUnitStatus = MemoryUnitStatus.UNIT_CANDIDATE
    title: str
    content: str
    context: str = ""
    importance_score: float = 0.0
    confidence_score: float = 0.0
    temporal_type: MemoryTemporalType = MemoryTemporalType.UNKNOWN
    valid_from: str | None = None
    valid_to: str | None = None
    superseded_by: int | None = None
    labels: list[str] = Field(default_factory=list)
    sources: list[MemoryUnitSource] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str | None = None
    recall_count: int = 0
    last_recalled_at: str | None = None
    why: str | None = None
    how_to_apply: str | None = None
    next_step: str | None = None


class HistorySessionRecord(BaseModel):
    """A normalized history session as produced by an importer.

    Attributes:
        id: Stable text ID prefixed by ``source_kind``
            (e.g. ``codex-jsonl:019e88ee-…``).
        external_id: Upstream session ID (synthesized when absent upstream).
        project: Project scope; CLI ``--project`` overrides importer inference.
        title: Human-readable label (e.g. upstream cwd or first user line).
        started_at: ISO-8601 start time, or None when unknown.
        ended_at: ISO-8601 end time, or None when unknown.
        source_kind: Origin format of the session.
        source_ref: How to find the upstream source again (file path).
    """

    id: str
    external_id: str
    project: str = ""
    title: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    source_kind: SourceKind
    source_ref: str


class HistoryMessageRecord(BaseModel):
    """A normalized session message as produced by an importer.

    ``text`` carries the full untruncated payload; the store layer applies
    secret redaction and the ``history_text_cap_bytes`` preview cap before
    anything is persisted or indexed.

    Attributes:
        id: Stable text ID (``{source_kind}:{session_external_id}:msg:{external_id}``).
        session_id: Parent session's stable ID.
        external_id: Upstream message ID, or a synthesized stable hash.
        role: Speaker role (``user``, ``assistant``, ``developer``, …).
        agent_id: Subagent identifier when applicable, else empty.
        parent_id: Stable ID of the parent message (threads), or None.
        seq: Display order within the session (may refresh on re-import).
        text: Full message text (uncapped; capped only at the store boundary).
        created_at: ISO-8601 timestamp, or None when the upstream lacks one.
    """

    id: str
    session_id: str
    external_id: str
    role: str
    agent_id: str = ""
    parent_id: str | None = None
    seq: int = 0
    text: str = ""
    created_at: str | None = None


class HistoryEventRecord(BaseModel):
    """A normalized non-message event (tool call/result, …) from an importer.

    Event identity is deterministic and never derived from file position:
    ``anchor`` is the upstream call ID when one exists, else
    ``{message_external_id}#{ordinal}`` where ``ordinal`` is the event's index
    within its parent message.  Both survive upstream file rewrites.

    Attributes:
        id: Stable text ID
            (``{source_kind}:{session_external_id}:evt:{anchor}:{kind}``).
        session_id: Parent session's stable ID.
        message_id: Stable ID of the parent message, or None.
        message_external_id: Upstream ID of the parent message (for locators).
        anchor: Upstream call ID or ``{message_external_id}#{ordinal}``.
        kind: Event kind (see :class:`HistoryEventKind`).
        tool_name: Tool name for tool events, else None.
        file_path: File touched by the event when known, else None.
        ordinal: Event index within its parent message (ordering only).
        body: Full event payload (uncapped; capped only at the store boundary).
        is_error: True when the event represents a failed tool result.
        created_at: ISO-8601 timestamp, or None when the upstream lacks one.
    """

    id: str
    session_id: str
    message_id: str | None = None
    message_external_id: str = ""
    anchor: str
    kind: HistoryEventKind = HistoryEventKind.OTHER
    tool_name: str | None = None
    file_path: str | None = None
    ordinal: int = 0
    body: str = ""
    is_error: bool = False
    created_at: str | None = None


class HistoryImportBatch(BaseModel):
    """Everything one importer pass produced from a single source file.

    Attributes:
        session: The session record (one source file = one session).
        messages: Normalized messages in file order.
        events: Normalized events in file order.
        next_offset_bytes: Byte offset after the last fully parsed line;
            stored as incremental-import state so re-importing a grown log
            resumes here.
        next_seq: Next message ``seq`` value, fed back in on resume.
    """

    session: HistorySessionRecord
    messages: list[HistoryMessageRecord] = Field(default_factory=list)
    events: list[HistoryEventRecord] = Field(default_factory=list)
    next_offset_bytes: int = 0
    next_seq: int = 0


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


class ExtractedUnit(BaseModel):
    """A memory unit extracted by an LLM/agent from one document.

    Mirrors the agent-facing subset of :class:`MemoryUnitRecord`: enough
    information to ingest a unit without going through deterministic
    checkpoint triage.  ``why``/``how_to_apply``/``next_step`` are optional
    so callers can emit partial rationale; the consolidation validator
    flags units that should have them but do not.
    """

    unit_type: MemoryUnitType
    title: str
    content: str
    context: str = ""
    why: str | None = None
    how_to_apply: str | None = None
    next_step: str | None = None
    labels: list[str] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: list[str]) -> list[str]:
        """Reject labels outside the closed :data:`MEMORY_LABELS` set.

        Surfaces as :class:`pydantic.ValidationError` during
        ``ExtractedGraph.model_validate`` so ``membox ingest-graph`` can
        produce a clear retriable error (no traceback) for agent output
        that violates the closed label vocabulary.
        """
        unknown = sorted(set(value) - MEMORY_LABELS)
        if unknown:
            msg = f"unknown memory labels: {', '.join(unknown)}"
            raise ValueError(msg)
        return value


class ExtractedGraph(BaseModel):
    """Full extraction result: entities, relations, and memory units.

    ``units`` defaults to an empty list so existing M3 JSON payloads
    (entities/relations only) keep validating unchanged.
    """

    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    units: list[ExtractedUnit] = Field(default_factory=list)


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
    superseded_by: int | None = None


class Document(BaseModel):
    """A source document stored for evidence lineage."""

    id: int
    content: str
    source: str = ""
    created_at: str = ""
    project: str | None = None
    source_path: str | None = None
    section: str | None = None
    doc_date: str | None = None
    version: int | None = None


class DocumentChunk(BaseModel):
    """A single markdown section chunk ready for ingestion.

    Produced by :func:`~membox.core.chunking.chunk_markdown` and consumed by
    :meth:`~membox.core.agent.MemoryAgent.ingest_file`.
    """

    section: str | None = Field(
        default=None,
        description="Section heading text (without ## prefix), or None for preamble.",
    )
    content: str = Field(description="Body text of this section.")


class IngestMetadata(BaseModel):
    """Metadata attached to a file-level ingest operation.

    Passed from the CLI layer to :meth:`~membox.core.agent.MemoryAgent.ingest_file`.
    """

    project: str | None = Field(
        default=None,
        description="Repository / directory name for --project scoping.",
    )
    source_path: str | None = Field(
        default=None,
        description="Canonical file path of the originating document.",
    )
    doc_date: str | None = Field(
        default=None,
        description="ISO-8601 date of the document snapshot (e.g. 2026-06-09).",
    )


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
