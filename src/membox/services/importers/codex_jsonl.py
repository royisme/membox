"""Importer for Codex CLI rollout logs (``codex`` adapt format).

Codex stores one session per file under ``~/.codex/sessions/YYYY/MM/DD/
rollout-<timestamp>-<uuid>.jsonl``.  Every line is
``{"timestamp": ..., "type": ..., "payload": {...}}``; the payload types this
adapter maps (verified against CLI 0.135.0 logs):

- ``session_meta`` → the session record (``payload.id`` is the session UUID,
  ``payload.cwd`` becomes the title and the project inference basis).
- ``response_item`` / ``message`` → a message (role + content-part texts).
- ``response_item`` / ``function_call`` → a ``tool_call`` event keyed by the
  upstream ``call_id``.
- ``response_item`` / ``function_call_output`` → a ``tool_result`` event
  keyed by the same ``call_id``.
- ``response_item`` / ``reasoning`` → skipped (content is encrypted).
- ``event_msg`` / ``turn_context`` / ``token_count`` etc. → skipped; their
  conversational content duplicates ``response_item`` records.

Codex messages carry no upstream ID, so external IDs are synthesized content
hashes (stable across re-imports, independent of file position).
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
from membox.services.importers.common import iter_jsonl, opt_str, synth_external_id

if TYPE_CHECKING:
    from pathlib import Path


def _payload_text(payload: dict[str, object]) -> str:
    """Concatenate the text parts of a Codex message payload."""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


class CodexJsonlImporter:
    """Parses Codex CLI rollout session logs."""

    format_name = "codex"

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse one Codex rollout log, optionally resuming mid-file.

        Args:
            path: Source rollout ``.jsonl`` file.
            project: Project override; falls back to the basename of the
                session's recorded ``cwd``.
            offset_bytes: Byte offset to resume from.
            next_seq: First message ``seq`` to assign when resuming.
            session: Previously imported session, required when resuming past
                the ``session_meta`` line.

        Returns:
            Normalized batch with resume state.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If no ``session_meta`` record is found and none was
                supplied via ``session``.
        """
        messages: list[HistoryMessageRecord] = []
        events: list[HistoryEventRecord] = []
        seq = next_seq
        end_offset = offset_bytes
        seen_ids: set[str] = set()
        call_kind_ordinals: dict[tuple[str, str], int] = {}
        last_message_external = ""
        last_timestamp: str | None = None

        for record, _before, after in iter_jsonl(path, offset_bytes):
            end_offset = after
            timestamp = opt_str(record.get("timestamp"))
            if timestamp:
                last_timestamp = timestamp
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            rtype = record.get("type")

            if rtype == "session_meta":
                external = str(payload.get("id") or path.stem)
                cwd = opt_str(payload.get("cwd")) or ""
                inferred = PurePosixPath(cwd).name if cwd else ""
                session = HistorySessionRecord(
                    id=f"{SourceKind.CODEX_JSONL.value}:{external}",
                    external_id=external,
                    project=project or inferred,
                    title=cwd or path.stem,
                    started_at=opt_str(payload.get("timestamp")) or timestamp,
                    ended_at=None,
                    source_kind=SourceKind.CODEX_JSONL,
                    source_ref=str(path),
                )
                continue
            if session is None:
                continue
            prefix = f"{SourceKind.CODEX_JSONL.value}:{session.external_id}"
            if rtype != "response_item":
                continue
            ptype = payload.get("type")

            if ptype == "message":
                role = str(payload.get("role", ""))
                text = _payload_text(payload)
                ext = synth_external_id(role, timestamp, text, seen_ids)
                seen_ids.add(ext)
                last_message_external = ext
                messages.append(
                    HistoryMessageRecord(
                        id=f"{prefix}:msg:{ext}",
                        session_id=session.id,
                        external_id=ext,
                        role=role,
                        seq=seq,
                        text=text,
                        created_at=timestamp,
                    )
                )
                seq += 1
                continue

            if ptype in ("function_call", "function_call_output"):
                call_id = opt_str(payload.get("call_id"))
                kind = (
                    HistoryEventKind.TOOL_CALL
                    if ptype == "function_call"
                    else HistoryEventKind.TOOL_RESULT
                )
                if call_id is None:
                    counter_key = (last_message_external, kind.value)
                    ordinal = call_kind_ordinals.get(counter_key, 0)
                    call_kind_ordinals[counter_key] = ordinal + 1
                    anchor = f"{last_message_external}#{ordinal}"
                else:
                    anchor = call_id
                    ordinal = 0
                if ptype == "function_call":
                    tool_name = opt_str(payload.get("name"))
                    body = str(payload.get("arguments", ""))
                    is_error = False
                else:
                    tool_name = None
                    output = payload.get("output")
                    body = output if isinstance(output, str) else str(output or "")
                    head = body[:200].lstrip()
                    is_error = head.startswith(("Error", "error:", '{"error'))
                events.append(
                    HistoryEventRecord(
                        id=f"{prefix}:evt:{anchor}:{kind.value}",
                        session_id=session.id,
                        message_id=(
                            f"{prefix}:msg:{last_message_external}"
                            if last_message_external
                            else None
                        ),
                        message_external_id=last_message_external,
                        anchor=anchor,
                        kind=kind,
                        tool_name=tool_name,
                        ordinal=ordinal,
                        body=body,
                        is_error=is_error,
                        created_at=timestamp,
                    )
                )

        if session is None:
            msg = f"{path}: no session_meta record found"
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

    def discover_sessions(self, project_cwd: Path, session_root: Path) -> list[Path]:
        """Return an empty list — Codex discovery is not yet implemented."""
        return []
