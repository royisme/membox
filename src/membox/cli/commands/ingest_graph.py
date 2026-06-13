"""`membox ingest-graph` — accept an agent-produced ExtractedGraph JSON.

Bypasses the LLM extractor: the calling agent has already performed
extraction; membox validates the JSON and writes it to the store via
``MemoryAgent.ingest_extracted``.  When the graph also carries an
``units`` array, those memory units are persisted as
:class:`~membox.model.schema.MemoryUnitRecord` rows too.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import ValidationError

from membox.cli._common import make_agent
from membox.core.project import infer_project
from membox.model.schema import (
    ExtractedGraph,
    ExtractedUnit,
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
)

if TYPE_CHECKING:
    from membox.core.store import KnowledgeStore

_DB_OPTION = typer.Option("memory.db", "--db", help="Path to SQLite database file")


def _read_payload(source: str) -> str:
    """Read the ExtractedGraph JSON payload from ``-`` (stdin) or a file path.

    Args:
        source: Either ``"-"`` (stdin) or a filesystem path.

    Returns:
        Raw JSON text.

    Raises:
        typer.Exit: Exits with code 1 when the file does not exist.
    """
    if source == "-":
        return sys.stdin.read()
    path = Path(source)
    if not path.exists():
        typer.echo(f"Error: file not found: {source}", err=True)
        raise typer.Exit(1)
    return path.read_text(encoding="utf-8")


def _parse_graph(raw: str) -> ExtractedGraph:
    """Parse and validate an ExtractedGraph JSON string.

    Args:
        raw: Raw JSON text expected to match the ExtractedGraph schema.

    Returns:
        Validated ``ExtractedGraph``.

    Raises:
        typer.Exit: Exits with code 1 on JSON or schema errors, with a clear
            retriable message.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(
            f"Error: invalid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno}). "
            "Re-run `membox extract-prompt` and feed its raw JSON to `ingest-graph`.",
            err=True,
        )
        raise typer.Exit(1) from None
    try:
        return ExtractedGraph.model_validate(data)
    except ValidationError as exc:
        typer.echo(
            "Error: invalid ExtractedGraph JSON: "
            f"{exc.error_count()} validation error(s); first: {exc.errors()[0]['msg']}. "
            "Re-run `membox extract-prompt` and feed valid JSON to `ingest-graph`.",
            err=True,
        )
        raise typer.Exit(1) from None


def ingest_graph(
    from_json: str = typer.Option(
        ...,
        "--from-json",
        help="Path to ExtractedGraph JSON, or '-' for stdin",
    ),
    source: str = typer.Option(
        ...,
        "--source",
        help=(
            "Source document path/label. When this is an existing readable "
            "file, its content is used as the document text; otherwise it "
            "is stored as a provenance label only."
        ),
    ),
    section: str | None = typer.Option(None, "--section", help="Section heading"),
    doc_date: str | None = typer.Option(
        None, "--doc-date", help="ISO-8601 date of the document snapshot (YYYY-MM-DD)"
    ),
    project: str | None = typer.Option(None, "--project", help="Project scope"),
    db: str = _DB_OPTION,
) -> None:
    """Validate an ExtractedGraph JSON and store it.

    The agent has already extracted the graph; membox validates the shape,
    then writes entities + relations to the SQLite store via
    :meth:`MemoryAgent.ingest_extracted`.  ``--source`` doubles as both the
    document text (when it is a real file) and the provenance label.

    Args:
        from_json: Path to the ExtractedGraph JSON, or ``"-"`` for stdin.
        source: Document path or provenance label.
        section: Optional section heading.
        doc_date: Optional ISO-8601 date string.
        project: Optional project scope.
        db: Path to the SQLite database file.
    """
    raw = _read_payload(from_json)
    graph = _parse_graph(raw)

    source_path = Path(source)
    text = source_path.read_text(encoding="utf-8") if source_path.is_file() else ""
    source_path_arg = str(source_path) if source_path.is_file() else None

    # ingest-graph bypasses the LLM by design (the agent already extracted),
    # so suppress the no-extractor footgun warning — it would be a confusing
    # self-reference here, not a missing-LLM signal.
    agent = make_agent(db, no_llm=True, warn_no_extractor=False)
    result = agent.ingest_extracted(
        text,
        graph,
        source=source,
        project=project,
        source_path=source_path_arg,
        section=section,
        doc_date=doc_date,
    )
    fed_entities = len(graph.entities)
    fed_relations = len(graph.relations)
    fed_units = len(graph.units)
    stored_entities = int(result["entities"])
    stored_relations = int(result["relations"])
    doc_id = int(result["doc_id"])
    stored_units = _ingest_graph_units(
        agent.store,
        graph.units,
        project=project or infer_project(Path.cwd() / "_"),
        source=source,
    )
    typer.echo(
        f"ingest-graph: stored {stored_entities} entities, "
        f"{stored_relations} relations, {stored_units} units (doc_id={doc_id}); "
        f"fed {fed_entities}/{fed_relations}/{fed_units}"
    )


def _ingest_graph_units(
    store: KnowledgeStore,
    units: Iterable[ExtractedUnit],
    *,
    project: str,
    source: str,
) -> int:
    """Persist every ``ExtractedUnit`` in *graph.units* as a memory unit.

    The source row is always recorded as :attr:`MemorySourceKind.DOCUMENT`:
    these units are *extracted by the agent from a source document/text*, not
    asserted directly by the user. Using ``MANUAL`` here would be wrong — the
    promotion policy treats ``MANUAL`` as explicit user confirmation and would
    auto-promote every agent-ingested unit to crystal on the next consolidate,
    defeating the ≥3-independent-source durability bar crystals exist to
    enforce. ``DOCUMENT`` requires real promotion criteria (independent
    sources / high-confidence decision) to crystallize.

    Args:
        store: The :class:`KnowledgeStore` to write to.
        units: The ``graph.units`` iterable from the validated ``ExtractedGraph``.
        project: Project scope (explicit CLI value, or inferred from CWD).
        source: The user-supplied ``--source`` value (provenance label or
            file path).  Recorded as the unit's ``source_ref``.

    Returns:
        Number of units stored.

    Raises:
        pydantic.ValidationError: If any unit fails ``_validate_memory_unit``
            (unknown label, etc.).  Pydantic's ValidationError already
            surfaces a clear, retriable message in the CLI handler.
    """
    stored = 0
    source_kind = MemorySourceKind.DOCUMENT
    for index, extracted in enumerate(units):
        snippet = extracted.content[:300] if extracted.content else ""
        # Each unit gets a per-unit source_message_id so multiple units from
        # the same --source are NOT deduped against each other (the store
        # dedupes on (source_kind, source_ref, source_message_id)).  The
        # source_ref itself stays as the user-supplied --source per spec.
        unit_msg_id = f"unit:{index}:{extracted.title}"
        unit = MemoryUnitRecord(
            project=project,
            unit_type=extracted.unit_type,
            status=MemoryUnitStatus.ACTIVE_UNIT,
            title=extracted.title,
            content=extracted.content,
            context=extracted.context,
            importance_score=0.5,
            confidence_score=0.7,
            why=extracted.why,
            how_to_apply=extracted.how_to_apply,
            next_step=extracted.next_step,
            labels=extracted.labels,
            sources=[
                MemoryUnitSource(
                    source_kind=source_kind,
                    source_ref=source,
                    source_message_id=unit_msg_id,
                    quote=snippet,
                )
            ],
        )
        store.create_memory_unit(unit)
        stored += 1
    return stored


__all__ = ["ingest_graph"]
