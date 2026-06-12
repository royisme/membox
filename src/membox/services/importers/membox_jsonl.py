"""Importer for ``membox`` — the normalized fixture adapt format.

One JSON object per line.  The first line must be the session header; later
lines are messages and events in conversation order:

.. code-block:: json

    {"type": "session", "id": "s1", "project": "demo", "title": "...",
     "started_at": "2026-06-11T00:00:00Z", "source_kind": "membox-capture"}
    {"type": "message", "id": "m1", "role": "user", "text": "...",
     "created_at": "..."}
    {"type": "event", "message_id": "m1", "kind": "tool_call",
     "tool_name": "bash", "call_id": "c1", "body": "..."}

``--format`` names the file format being parsed; ``source_kind`` records the
origin and defaults to ``membox-capture`` unless the session header specifies
its own.  Event identity uses the upstream ``call_id`` when present, else the
event's ordinal within its parent message — never the file line number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from membox.model.schema import (
    HistoryEventKind,
    HistoryEventRecord,
    HistoryImportBatch,
    HistoryMessageRecord,
    HistorySessionRecord,
    SourceKind,
)
from membox.services.importers.common import iter_jsonl, opt_str, synth_external_id

if TYPE_CHECKING:
    from pathlib import Path


class MemboxHistoryJsonlImporter:
    """Parses the normalized ``membox`` fixture adapt format."""

    format_name = "membox"

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse one fixture log, optionally resuming mid-file.

        Args:
            path: Source ``.jsonl`` file.
            project: Project override; falls back to the session header's
                ``project`` field, then to empty.
            offset_bytes: Byte offset to resume from.
            next_seq: First message ``seq`` to assign when resuming.
            session: Previously imported session, required when resuming past
                the session header line.

        Returns:
            Normalized batch with resume state.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If the log lacks a session header and none was
                supplied via ``session``.
        """
        messages: list[HistoryMessageRecord] = []
        events: list[HistoryEventRecord] = []
        seq = next_seq
        end_offset = offset_bytes
        seen_ids: set[str] = set()
        # ordinal counter per parent message, for events without a call_id
        event_ordinals: dict[str, int] = {}
        last_message_external = ""
        last_message_id: str | None = None
        last_message_created: str | None = None

        for record, _offset_before, offset_after in iter_jsonl(path, offset_bytes):
            end_offset = offset_after
            rtype = record.get("type")
            if rtype == "session":
                kind = SourceKind(str(record.get("source_kind", "membox-capture")))
                external = str(record.get("id", path.stem))
                session = HistorySessionRecord(
                    id=f"{kind.value}:{external}",
                    external_id=external,
                    project=project or str(record.get("project", "")),
                    title=str(record.get("title", "")),
                    started_at=opt_str(record.get("started_at")),
                    ended_at=opt_str(record.get("ended_at")),
                    source_kind=kind,
                    source_ref=str(path),
                )
                continue
            if session is None:
                msg = f"{path}: first record must be a session header"
                raise ValueError(msg)
            prefix = f"{session.source_kind.value}:{session.external_id}"

            if rtype == "message":
                text = str(record.get("text", ""))
                role = str(record.get("role", ""))
                created = opt_str(record.get("created_at")) or session.started_at
                upstream_id = record.get("id")
                ext = (
                    str(upstream_id)
                    if upstream_id is not None
                    else synth_external_id(role, record.get("created_at"), text, seen_ids)
                )
                seen_ids.add(ext)
                last_message_external = ext
                last_message_id = f"{prefix}:msg:{ext}"
                last_message_created = created
                messages.append(
                    HistoryMessageRecord(
                        id=last_message_id,
                        session_id=session.id,
                        external_id=ext,
                        role=role,
                        agent_id=str(record.get("agent_id", "")),
                        parent_id=(
                            f"{prefix}:msg:{record['parent_id']}"
                            if record.get("parent_id")
                            else None
                        ),
                        seq=seq,
                        text=text,
                        created_at=created,
                    )
                )
                seq += 1
                continue

            if rtype == "event":
                msg_ext = str(record.get("message_id") or last_message_external)
                ordinal = event_ordinals.get(msg_ext, 0)
                event_ordinals[msg_ext] = ordinal + 1
                anchor = str(record.get("call_id") or f"{msg_ext}#{ordinal}")
                kind_str = str(record.get("kind", "other"))
                try:
                    evt_kind = HistoryEventKind(kind_str)
                except ValueError:
                    evt_kind = HistoryEventKind.OTHER
                created = (
                    opt_str(record.get("created_at")) or last_message_created or session.started_at
                )
                events.append(
                    HistoryEventRecord(
                        id=f"{prefix}:evt:{anchor}:{evt_kind.value}",
                        session_id=session.id,
                        message_id=(f"{prefix}:msg:{msg_ext}" if msg_ext else last_message_id),
                        message_external_id=msg_ext,
                        anchor=anchor,
                        kind=evt_kind,
                        tool_name=opt_str(record.get("tool_name")),
                        file_path=opt_str(record.get("file_path")),
                        ordinal=ordinal,
                        body=str(record.get("body", "")),
                        is_error=bool(record.get("is_error", False)),
                        created_at=created,
                    )
                )

        if session is None:
            msg = f"{path}: no session header found"
            raise ValueError(msg)
        return HistoryImportBatch(
            session=session,
            messages=messages,
            events=events,
            next_offset_bytes=end_offset,
            next_seq=seq,
        )

    def discover_sessions(self, project_cwd: Path, session_root: Path) -> list[Path]:
        """Return an empty list — membox fixture format has no discovery layout."""
        return []
