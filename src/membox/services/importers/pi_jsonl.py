"""Importer for Pi coding agent session logs (``pi-jsonl``).

Pi stores one session per file under
``~/.pi/agent/sessions/<project-path>/<timestamp>_<uuid>.jsonl``.
Every line is a JSON object with ``type``, ``id``, ``parentId``, and
``timestamp`` at the top level.

Record types this adapter maps:

- ``session`` → the session record (``id`` is the session UUID,
  ``cwd`` becomes the title and project-inference basis).
- ``message`` → a message, with ``message.role`` in
  ``{user, assistant, toolResult}`` and ``message.content`` as an array
  of content parts:
  - ``{type: "text", text: "..."}`` — text content.
  - ``{type: "thinking", thinking: "...", ...}`` — skipped (reasoning
    content is encrypted/not useful for recall).
  - ``{type: "toolCall", id, name, arguments}`` → a ``tool_call`` event.
  - ``{type: "toolResult", toolCallId, content, isError}`` → embedded
    in ``toolResult``-role messages.
- ``model_change`` / ``thinking_level_change`` / ``custom`` /
  ``compaction`` → skipped.

Pi message IDs are stable upstream UUIDs, so external IDs use the
upstream ``id`` directly — no synthetic content hashes needed.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from membox.model.schema import (
    HistoryEventKind,
    HistoryEventRecord,
    HistoryImportBatch,
    HistoryMessageRecord,
    HistorySessionRecord,
    SourceKind,
)
from membox.services.importers.common import iter_jsonl, opt_str

if TYPE_CHECKING:
    from pathlib import Path


def _message_text(content: list[dict[str, object]]) -> str:
    """Concatenate text parts of a Pi message content array, skipping non-text."""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _message_text_from_obj(content: object) -> str:
    """Extract text from either a string or Pi content-part array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _message_text(content)
    return ""


class PiJsonlImporter:
    """Parses Pi coding agent session logs."""

    format_name = "pi-jsonl"

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse one Pi session log, optionally resuming mid-file.

        Args:
            path: Source ``.jsonl`` file.
            project: Project override; falls back to the basename of the
                session's recorded ``cwd``.
            offset_bytes: Byte offset to resume from.
            next_seq: First message ``seq`` to assign when resuming.
            session: Previously imported session, required when resuming
                past the session header line.

        Returns:
            Normalized batch with resume state.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If no ``session`` record is found and none was
                supplied via ``session``.
        """
        messages: list[HistoryMessageRecord] = []
        events: list[HistoryEventRecord] = []
        seq = next_seq
        end_offset = offset_bytes
        last_message_id: str | None = None
        last_timestamp: str | None = None

        for record, _offset_before, offset_after in iter_jsonl(path, offset_bytes):
            end_offset = offset_after
            timestamp = opt_str(record.get("timestamp"))
            if timestamp:
                last_timestamp = timestamp
            rtype = record.get("type")

            if rtype == "session":
                external = str(record.get("id") or path.stem)
                cwd = opt_str(record.get("cwd")) or ""
                inferred = PurePosixPath(cwd).name if cwd else ""
                session = HistorySessionRecord(
                    id=f"{SourceKind.PI_JSONL.value}:{external}",
                    external_id=external,
                    project=project or inferred,
                    title=cwd or path.stem,
                    started_at=timestamp,
                    ended_at=None,
                    source_kind=SourceKind.PI_JSONL,
                    source_ref=str(path),
                )
                continue
            if session is None:
                continue
            if rtype != "message":
                continue

            prefix = f"{SourceKind.PI_JSONL.value}:{session.external_id}"
            msg = record.get("message")
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", ""))
            content = msg.get("content", [])
            created = opt_str(msg.get("timestamp"))
            pi_msg_id = str(record.get("id", ""))

            if role == "toolResult":
                # Tool results are separate messages in Pi.
                tc_id = opt_str(msg.get("toolCallId")) or ""
                tool_name = opt_str(msg.get("toolName"))
                is_error = bool(msg.get("isError", False))
                body = _message_text_from_obj(content)
                events.append(
                    HistoryEventRecord(
                        id=f"{prefix}:evt:{tc_id}:{HistoryEventKind.TOOL_RESULT.value}",
                        session_id=session.id,
                        message_id=last_message_id,
                        message_external_id="",
                        anchor=tc_id,
                        kind=HistoryEventKind.TOOL_RESULT,
                        tool_name=tool_name,
                        ordinal=0,
                        body=body,
                        is_error=is_error,
                        created_at=timestamp,
                    )
                )
                continue

            # User and assistant messages.
            text = _message_text_from_obj(content)
            messages.append(
                HistoryMessageRecord(
                    id=f"{prefix}:msg:{pi_msg_id}",
                    session_id=session.id,
                    external_id=pi_msg_id,
                    role=role,
                    seq=seq,
                    text=text,
                    created_at=created or timestamp,
                )
            )
            last_message_id = f"{prefix}:msg:{pi_msg_id}"
            seq += 1

            # Extract tool calls from assistant message content parts.
            if not isinstance(content, list):
                continue
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "toolCall":
                    continue
                tc_id = opt_str(part.get("id")) or f"{pi_msg_id}-tc{part_idx}"
                tool_name = opt_str(part.get("name"))
                args = part.get("arguments")
                body = str(args) if args is not None else ""
                events.append(
                    HistoryEventRecord(
                        id=f"{prefix}:evt:{tc_id}:{HistoryEventKind.TOOL_CALL.value}",
                        session_id=session.id,
                        message_id=last_message_id,
                        message_external_id=pi_msg_id,
                        anchor=tc_id,
                        kind=HistoryEventKind.TOOL_CALL,
                        tool_name=tool_name,
                        ordinal=part_idx,
                        body=body,
                        is_error=False,
                        created_at=timestamp,
                    )
                )

        if session is None:
            msg = f"{path}: no session record found"
            raise ValueError(msg)
        if session.ended_at is None and last_timestamp:
            session = session.model_copy(update={"ended_at": last_timestamp})
        return HistoryImportBatch(
            session=session,
            messages=messages,
            events=events,
            next_offset_bytes=end_offset,
            next_seq=seq,
        )
