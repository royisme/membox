"""History import orchestration and payload fetch (lifecycle Phase B).

Bridges the parsing layer (:mod:`membox.services.importers`) and the storage
layer (:class:`~membox.core.store.history.HistoryOps`):

- :func:`import_history` runs one importer pass with per-source incremental
  state, so re-importing a grown log resumes from the stored byte offset and
  re-importing an unchanged log is a no-op.
- :func:`fetch_payload` resolves a stored row's identity-based
  ``payload_locator`` back to the upstream log and returns the full payload —
  raw upstream content, read fresh, never persisted or indexed by Membox.
  When the upstream file is gone it reports that honestly instead of falling
  back to the preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from membox.model.schema import HistorySessionRecord, SourceKind
from membox.services.importers import get_importer
from membox.services.importers.common import count_malformed_jsonl_lines

if TYPE_CHECKING:
    from membox.core.store import KnowledgeStore
    from membox.core.store.history import ImportState

_FORMAT_BY_SOURCE_KIND: dict[str, str] = {
    SourceKind.CODEX_JSONL.value: "codex",
    SourceKind.CLAUDE_JSONL.value: "claude",
    SourceKind.PI_JSONL.value: "pi",
    SourceKind.MEMBOX_CAPTURE.value: "membox",
    SourceKind.MANUAL.value: "membox",
}
"""``source_kind`` → importer ``--adapt`` name used to re-parse for fetch."""


class ImportResult(TypedDict):
    """Summary of one :func:`import_history` pass."""

    session_id: str
    messages: int
    events: int
    skipped_lines: int
    skipped: bool


class FetchResult(TypedDict):
    """Result of one :func:`fetch_payload` call."""

    found: bool
    payload: str
    note: str


class PullResult(TypedDict):
    """Summary of one :func:`history_pull` call."""

    sessions: int
    messages: int
    events: int
    skipped_lines: int
    files: list[str]


def history_pull(
    store: KnowledgeStore,
    format_name: str,
    *,
    project: str | None = None,
    session_root: Path,
    text_cap_bytes: int = 16384,
) -> PullResult:
    """Auto-discover and import all session logs for the current project.

    Uses the adapter's ``discover_sessions`` to find session files matching
    the current working directory, then imports each one via
    :func:`import_history` (incremental + idempotent).

    Args:
        store: Open knowledge store.
        format_name: Importer ``--adapt`` name.
        project: Project scope override.
        session_root: Agent session storage root directory.
        text_cap_bytes: Preview cap passed to ``import_history``.

    Returns:
        Pull summary with counts and file list.

    Raises:
        ValueError: If ``format_name`` is unknown or the adapter doesn't
            support session discovery.
    """
    importer = get_importer(format_name)
    project_cwd = Path.cwd()

    discovered = importer.discover_sessions(project_cwd, session_root)
    if not discovered:
        return PullResult(sessions=0, messages=0, events=0, skipped_lines=0, files=[])

    total_msg = 0
    total_evt = 0
    total_skipped_lines = 0
    imported_files: list[str] = []
    for path in discovered:
        result = import_history(
            store,
            path,
            format_name,
            project=project,
            text_cap_bytes=text_cap_bytes,
        )
        if not result["skipped"]:
            total_msg += result["messages"]
            total_evt += result["events"]
            total_skipped_lines += result["skipped_lines"]
            imported_files.append(str(path))

    return PullResult(
        sessions=len(discovered),
        messages=total_msg,
        events=total_evt,
        skipped_lines=total_skipped_lines,
        files=imported_files,
    )


def import_history(
    store: KnowledgeStore,
    path: Path,
    format_name: str,
    *,
    project: str | None = None,
    text_cap_bytes: int = 16384,
) -> ImportResult:
    """Import one session log with incremental per-source state.

    A source whose size and mtime are unchanged since the last import is
    skipped outright.  A grown log resumes from the stored byte offset; a
    shrunk or rewritten log (offset beyond EOF) is re-parsed from the start —
    existing rows upsert in place and rows for vanished upstream lines are
    kept (append-only trace).

    Args:
        store: Open knowledge store.
        path: Source log file.
        format_name: Importer ``--adapt`` name.
        project: Project override carried into the session record.
        text_cap_bytes: Preview cap (``HistoryConfig.text_cap_bytes``).

    Returns:
        Import summary; ``skipped`` is True for the unchanged-source no-op.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If ``format_name`` is unknown or the log is malformed.
    """
    resolved = path.resolve()
    if not resolved.exists():
        msg = f"File not found: {path}"
        raise FileNotFoundError(msg)
    importer = get_importer(format_name)
    source_ref = str(resolved)
    stat = resolved.stat()

    state = store.get_import_state(source_ref)
    offset = 0
    next_seq = 0
    prior_session: HistorySessionRecord | None = None
    if state is not None:
        if state["mtime"] == stat.st_mtime and state["size_bytes"] == stat.st_size:
            return ImportResult(
                session_id=state["session_id"] or "",
                messages=0,
                events=0,
                skipped_lines=0,
                skipped=True,
            )
        if 0 < state["offset_bytes"] <= stat.st_size and state["session_id"]:
            row = store.get_history_session(state["session_id"])
            if row is not None:
                prior_session = HistorySessionRecord(
                    id=str(row["id"]),
                    external_id=str(row["external_id"]),
                    project=str(row["project"]),
                    title=str(row["title"]),
                    started_at=row["started_at"]
                    if row["started_at"] is None
                    else str(row["started_at"]),
                    ended_at=row["ended_at"] if row["ended_at"] is None else str(row["ended_at"]),
                    source_kind=SourceKind(str(row["source_kind"])),
                    source_ref=str(row["source_ref"]),
                )
                offset = state["offset_bytes"]
                next_seq = state["next_seq"]

    batch = importer.parse(
        resolved,
        project=project,
        offset_bytes=offset,
        next_seq=next_seq,
        session=prior_session,
    )
    skipped_lines = count_malformed_jsonl_lines(resolved, offset)
    store.upsert_history_session(batch.session)
    n_msg = store.upsert_history_messages(
        batch.session, batch.messages, text_cap_bytes=text_cap_bytes
    )
    n_evt = store.upsert_history_events(batch.session, batch.events, text_cap_bytes=text_cap_bytes)
    new_state: ImportState = {
        "source_ref": source_ref,
        "source_kind": batch.session.source_kind.value,
        "project": batch.session.project,
        "session_id": batch.session.id,
        "mtime": stat.st_mtime,
        "size_bytes": stat.st_size,
        "offset_bytes": batch.next_offset_bytes,
        "next_seq": batch.next_seq,
    }
    store.set_import_state(new_state)
    return ImportResult(
        session_id=batch.session.id,
        messages=n_msg,
        events=n_evt,
        skipped_lines=skipped_lines,
        skipped=False,
    )


def fetch_payload(
    store: KnowledgeStore, record_id: str, *, project: str | None = None
) -> FetchResult:
    """Re-read the full payload of a message or event from its upstream log.

    Resolves the row's ``payload_locator`` (source_ref + external_id +
    optional ordinal), re-parses the upstream file with the session's
    importer, and returns the matching record's full text.  Output is raw
    upstream content; it is never persisted or indexed.

    Args:
        store: Open knowledge store.
        record_id: Stable message or event ID.
        project: Optional project scope guard.  When set, records from other
            projects are treated as not found.

    Returns:
        ``found=True`` with the full payload, or ``found=False`` with a
        ``note`` explaining honestly why (unknown ID, missing upstream file,
        record compacted away upstream).
    """
    row = store.get_history_record(record_id)
    if row is None:
        return FetchResult(found=False, payload="", note=f"no such history record: {record_id}")
    if project is not None and str(row["project"]) != project:
        return FetchResult(
            found=False,
            payload="",
            note=f"no such history record in project {project!r}: {record_id}",
        )
    locator_raw = row.get("payload_locator")
    session = store.get_history_session(str(row["session_id"]))
    if not locator_raw or session is None:
        return FetchResult(found=False, payload="", note="record has no payload locator")
    locator = json.loads(str(locator_raw))
    source_ref = str(locator.get("source_ref", ""))
    source_path = Path(source_ref)
    if not source_path.exists():
        return FetchResult(
            found=False,
            payload="",
            note=f"source no longer available: {source_ref}",
        )

    source_kind = str(session["source_kind"])
    format_name = _FORMAT_BY_SOURCE_KIND.get(source_kind)
    if format_name is None:
        return FetchResult(
            found=False, payload="", note=f"no fetch support for source kind {source_kind!r}"
        )
    importer = get_importer(format_name)
    batch = importer.parse(source_path)

    if row["trace_kind"] == "message":
        external_id = str(locator.get("external_id", ""))
        for msg in batch.messages:
            if msg.external_id == external_id:
                return FetchResult(found=True, payload=msg.text, note="")
    else:
        for evt in batch.events:
            if evt.id == record_id:
                return FetchResult(found=True, payload=evt.body, note="")
    return FetchResult(
        found=False,
        payload="",
        note=f"record no longer present in upstream source: {source_ref}",
    )
