"""Lifecycle orchestration: one-shot checkpoint = pull → triage → extract.

The checkpoint wrapper composes the three existing ``KnowledgeStore``-driven
steps behind a single service-layer entry point. The CLI command stays a
thin shell that parses arguments and prints a summary, so the same
function can later be invoked from an automatic (hook-driven) trigger
without diverging from the manual flow.

The dry-run path runs the identical pull→triage→extract sequence against
an ephemeral temp-file SQLite store, so the real database is never written
in preview mode and the two paths share a single implementation. The
preview is therefore a "fresh-store" count — it ignores the real db's
prior import-state and dedup, so it shows the full would-capture figure.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from membox.config import HistoryConfig
from membox.core.history_import import PullResult, history_pull
from membox.core.store import KnowledgeStore
from membox.core.triage import GATE_VERSION, activation_passes, triage_trace
from membox.model.schema import (
    HistoryTriageRecord,
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    TraceKind,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class LifecycleLeaseError(Exception):
    """Raised when the lifecycle lease for a project is held by another process.

    The CLI maps this to exit 1 with a clear "another lifecycle operation is
    in progress" message. Service-layer code does not exit on its own; the
    caller decides how to react.
    """


@dataclass
class CheckpointResult:
    """Summary of one :func:`run_checkpoint` invocation.

    Attributes:
        sessions_pulled: Number of session files pulled from the adapter.
        messages_pulled: Number of history messages written by the pull step.
        skipped_lines: Malformed JSONL lines skipped during pull (mirrors
            the existing ``history pull`` semantics: nonzero means exit 1).
        triaged_rows: Number of trace rows for which a triage decision was
            written (one per row inspected, regardless of should_extract).
        units_created: Number of memory units created (or covered) by the
            extract step. ``create_memory_unit`` returns the existing id
            when a source identity is already covered, so re-runs grow by 0.
        applied: ``True`` when changes were persisted to ``store``;
            ``False`` for the dry-run path which used an ephemeral store.
    """

    sessions_pulled: int
    messages_pulled: int
    skipped_lines: int
    triaged_rows: int
    units_created: int
    applied: bool


def run_checkpoint(
    store: KnowledgeStore,
    *,
    project: str,
    session_root: str | None,
    adapt: str,
    since: str | None,
    limit: int,
    apply: bool,
) -> CheckpointResult:
    """Run a one-shot checkpoint: pull → triage → extract.

    Args:
        store: Open knowledge store. Used as the target when ``apply`` is
            True; otherwise the function opens an ephemeral temp-file store
            and ``store`` is left untouched.
        project: Project scope for triage/extract and for the lease.
        session_root: Adapter session storage root. ``None`` is allowed
            when the user passes ``--session-root`` (or env var) but the
            pull returns zero — that path still surfaces "nothing new"
            messaging rather than raising.
        adapt: Importer ``--adapt`` name (one of ``IMPORTER_FORMATS``).
        since: ISO-8601 lower bound forwarded to triage and extract.
        limit: Maximum trace rows to triage and pending triage rows to
            extract (the extract half pulls only ``should_extract=1``).
        apply: When True, changes are persisted to ``store`` and the
            triage→extract span holds the lifecycle lease. When False,
            the identical sequence runs against a throwaway store and
            nothing is persisted to ``store``.

    Returns:
        The :class:`CheckpointResult` with counts from both paths and an
        ``applied`` flag distinguishing real runs from previews.

    Raises:
        LifecycleLeaseError: When ``apply`` is True and the lifecycle
            lease for ``project`` is held by another process. The CLI
            command maps this to exit 1.
    """
    if apply:
        return _apply_checkpoint(
            store,
            project=project,
            session_root=session_root,
            adapt=adapt,
            since=since,
            limit=limit,
        )
    return _dry_run_checkpoint(
        project=project,
        session_root=session_root,
        adapt=adapt,
        since=since,
        limit=limit,
    )


def _apply_checkpoint(
    store: KnowledgeStore,
    *,
    project: str,
    session_root: str | None,
    adapt: str,
    since: str | None,
    limit: int,
) -> CheckpointResult:
    """Real run: persist to ``store``; lease spans triage+extract."""
    if not store.acquire_lifecycle_lease(project):
        raise LifecycleLeaseError(project)
    try:
        return _run_chain(
            store,
            project=project,
            session_root=session_root,
            adapt=adapt,
            since=since,
            limit=limit,
            applied=True,
        )
    finally:
        store.release_lifecycle_lease(project)


def _dry_run_checkpoint(
    *,
    project: str,
    session_root: str | None,
    adapt: str,
    since: str | None,
    limit: int,
) -> CheckpointResult:
    """Preview: run against an ephemeral temp-file store; nothing persisted."""
    with tempfile.TemporaryDirectory() as tmp:
        ephemeral_db = str(Path(tmp) / "checkpoint-dryrun.db")
        ephemeral = KnowledgeStore(ephemeral_db)
        try:
            return _run_chain(
                ephemeral,
                project=project,
                session_root=session_root,
                adapt=adapt,
                since=since,
                limit=limit,
                applied=False,
            )
        finally:
            ephemeral.close()


def _run_chain(
    store: KnowledgeStore,
    *,
    project: str,
    session_root: str | None,
    adapt: str,
    since: str | None,
    limit: int,
    applied: bool,
) -> CheckpointResult:
    """The shared pull → triage → extract sequence.

    Same code for apply and dry-run; only the target store and the lease
    decision differ (handled by the callers).
    """
    text_cap_bytes = HistoryConfig().text_cap_bytes

    # 1. Pull. Pull itself does not need the lease (matches existing history
    #    pull semantics) and the lease acquired by the apply caller does not
    #    need to span it. When the caller has no ``session_root`` (neither
    #    ``--session-root`` nor ``$MEMBOX_SESSION_ROOT`` set) there is nothing
    #    to discover — surface 0 sessions / 0 messages / 0 skipped_lines and
    #    let triage/extract run over any already-pending rows (apply path).
    if session_root:
        resolved_root = Path(session_root)
        pull = history_pull(
            store,
            adapt,
            project=project,
            session_root=resolved_root,
            text_cap_bytes=text_cap_bytes,
        )
    else:
        pull = PullResult(sessions=0, messages=0, events=0, skipped_lines=0, files=[])

    # 2. Triage.
    rows = store.trace_rows_for_triage(project=project, since=since, limit=limit)
    triaged_rows = 0
    for row in rows:
        decision = triage_trace(
            row["text"],
            role=row["role"],
            trace_kind=row["trace_kind"],
        )
        store.upsert_history_triage(
            HistoryTriageRecord(
                project=project,
                trace_kind=TraceKind(row["trace_kind"]),
                trace_id=row["trace_id"],
                should_extract=decision.should_extract,
                unit_type=decision.unit_type,
                importance_score=decision.importance_score,
                confidence_score=decision.confidence_score,
                temporal_type=decision.temporal_type,
                user_intent=decision.user_intent,
                extraction_hint=decision.extraction_hint,
                reason=decision.reason,
                gate_version=GATE_VERSION,
            )
        )
        triaged_rows += 1

    # 3. Extract.
    triage_rows = store.pending_triage_rows(
        project=project,
        gate_version=GATE_VERSION,
        limit=limit,
    )
    consumed: list[int] = []
    units_created = 0
    for triage in triage_rows:
        trace = store.get_trace_text(triage.trace_kind.value, triage.trace_id)
        if trace is None:
            continue
        decision = triage_trace(
            trace["text"],
            role=trace["role"],
            trace_kind=trace["trace_kind"],
        )
        unit = _unit_from_trace(project, trace, decision)
        store.create_memory_unit(unit)
        if triage.id is not None:
            consumed.append(triage.id)
        units_created += 1
    store.mark_triage_consumed(consumed)

    return CheckpointResult(
        sessions_pulled=pull["sessions"],
        messages_pulled=pull["messages"],
        skipped_lines=pull["skipped_lines"],
        triaged_rows=triaged_rows,
        units_created=units_created,
        applied=applied,
    )


def _unit_from_trace(
    project: str,
    trace: Mapping[str, object],
    decision: object,
) -> MemoryUnitRecord:
    """Build a deterministic Phase C unit from one trace row.

    Mirrors the helper used by ``memory extract`` so the checkpoint wrapper
    produces identical units to the manual pipeline.
    """
    from membox.core.triage import GateDecision

    typed_decision = decision
    assert isinstance(typed_decision, GateDecision)
    source_kind = (
        MemorySourceKind.HISTORY_MESSAGE
        if trace["trace_kind"] == "message"
        else MemorySourceKind.HISTORY_EVENT
    )
    text = str(trace["text"]).strip()
    source = MemoryUnitSource(
        source_kind=source_kind,
        source_ref=str(trace["trace_id"]),
        source_message_id=str(trace["trace_id"]) if trace["trace_kind"] == "message" else "",
        quote=text[:300],
    )
    status = (
        MemoryUnitStatus.ACTIVE_UNIT
        if activation_passes(typed_decision, has_source=True)
        else MemoryUnitStatus.UNIT_CANDIDATE
    )
    return MemoryUnitRecord(
        project=project,
        unit_type=typed_decision.unit_type,
        status=status,
        title=typed_decision.extraction_hint or "Memory unit",
        content=text[:1200],
        context=f"Extracted from {trace['trace_kind']} {trace['trace_id']}",
        importance_score=typed_decision.importance_score,
        confidence_score=typed_decision.confidence_score,
        temporal_type=typed_decision.temporal_type,
        labels=typed_decision.labels,
        sources=[source],
    )
