"""Importer for Claude Code session logs (``claude`` adapt format).

Claude Code stores one session per ``.jsonl`` file under
``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`` where ``<encoded-cwd>`` is
the absolute project path with every ``/`` replaced by ``-`` (e.g.
``/Users/royzhu/proj`` → ``-Users-royzhu-proj``).  Each line is a JSON
object dispatched on top-level ``type``.

The mapping implemented here:

- ``assistant`` → message (``role="assistant"``) + events from ``tool_use``
  blocks; ``thinking`` blocks become :data:`HistoryEventKind.REASONING`
  events, not message text.
- ``user`` → message (``role="user"``) + events from ``tool_result``
  blocks.  Synthetic ``isMeta:true`` lines are skipped.
- ``system`` → message (``role="system"``).
- ``ai-title`` → session title source only — no message emitted.
- ``attachment``, ``mode``, ``permission-mode``, ``last-prompt``,
  ``file-history-snapshot``, ``queue-operation`` → skipped (control lines).

There is no session-start header.  ``session.external_id`` is the upstream
``sessionId``; ``started_at`` / ``ended_at`` are the first / last chat line
``timestamp``; ``title`` is the last ``ai-title`` (else first user text
snippet); ``project`` is the override or the basename of the line ``cwd``.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePath, PurePosixPath

from membox.model.schema import (
    HistoryEventKind,
    HistoryEventRecord,
    HistoryImportBatch,
    HistoryMessageRecord,
    HistorySessionRecord,
    SourceKind,
)
from membox.services.importers.common import iter_jsonl, opt_str

# Top-level ``type`` values that carry no conversational content.  They
# appear regularly in real Claude logs and must be skipped silently.
_CONTROL_LINE_TYPES: frozenset[str] = frozenset(
    {
        "attachment",
        "mode",
        "permission-mode",
        "last-prompt",
        "file-history-snapshot",
        "queue-operation",
    },
)


def _claude_project_dirname(project_cwd: PurePath) -> str:
    """Encode ``project_cwd`` to Claude's per-project directory name.

    Claude Code stores sessions in
    ``~/.claude/projects/<encoded>/<uuid>.jsonl`` where ``<encoded>`` is the
    absolute project path with every ``/`` replaced by ``-`` (so a leading
    ``/`` becomes a leading ``-``).

    Args:
        project_cwd: Absolute path to the project working directory.

    Returns:
        The encoded directory name, e.g. ``-Users-royzhu-proj``.
    """
    raw = str(project_cwd).replace("\\", "/")
    if raw.startswith("~"):
        raw = str(Path(raw).expanduser()).replace("\\", "/")
    elif not (raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":")):
        raw = str(Path(raw).resolve()).replace("\\", "/")
    normalized = raw
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[2:]
    return normalized.replace("/", "-")


def _message_text_from_obj(content: object) -> str:
    """Concatenate the ``text`` blocks of a Claude message ``content`` array.

    ``tool_use``, ``tool_result`` and ``thinking`` blocks are intentionally
    skipped — they are emitted as separate events.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _stringify_block_content(content: object) -> str:
    """Stringify a ``tool_result`` ``content`` field, which may be string or array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _tool_input_file_path(tool_name: str | None, tool_input: object) -> str | None:
    """Extract the file path from a tool_use ``input`` payload, when reliable."""
    if tool_name not in ("Read", "Edit", "Write"):
        return None
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    return path if isinstance(path, str) else None


def _parent_depth_map(records: list[dict[str, object]]) -> dict[str, int]:
    """Compute parent-chain depth for every chat line in *records*.

    Used to break timestamp ties when assigning ``seq`` deterministically
    without depending on file position.  The root (parentUuid null/None)
    has depth 0; children of a depth-N parent get depth N+1.
    """
    depth: dict[str, int] = {}
    for rec in records:
        uuid = opt_str(rec.get("uuid"))
        parent = rec.get("parentUuid")
        if uuid is None:
            continue
        if parent is None or parent == "" or not isinstance(parent, str):
            depth[uuid] = 0
            continue
        depth[uuid] = depth.get(parent, 0) + 1
    return depth


class ClaudeJsonlImporter:
    """Parses Claude Code session JSONL logs."""

    format_name = "claude"

    def discover_sessions(self, project_cwd: Path, session_root: Path) -> list[Path]:
        """Return Claude session ``.jsonl`` files for *project_cwd* under *session_root*.

        Claude stores sessions in ``<session_root>/<encoded-cwd>/*.jsonl``.
        Returns an empty list if the encoded directory does not exist.
        """
        root = session_root.expanduser().resolve()
        if not root.is_dir():
            return []
        encoded = _claude_project_dirname(project_cwd)
        project_dir = root / encoded
        if not project_dir.is_dir():
            return []
        return sorted(project_dir.glob("*.jsonl"))

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse one Claude session log, optionally resuming mid-file.

        Args:
            path: Source ``.jsonl`` file.
            project: Project override; falls back to the basename of the
                first chat line's ``cwd``.
            offset_bytes: Byte offset to resume from (0 = beginning).
            next_seq: First message ``seq`` to assign when resuming.
            session: Previously imported session, required when resuming.

        Returns:
            Normalized batch with resume state.

        Raises:
            FileNotFoundError: If *path* does not exist.
            ValueError: If no chat lines are found and no *session* was
                supplied.
        """
        messages: list[HistoryMessageRecord] = []
        events: list[HistoryEventRecord] = []

        # Pre-declared so the first-pass ``ai-title`` scan can write to it
        # without creating a new local binding that shadows later uses.
        title: str | None = None

        # First pass: collect chat-line records in file order so we can
        # compute parent-chain depths (tie-break) and session metadata
        # (started_at / ended_at / cwd) before assigning ``seq``.  Also
        # scan the whole file for ``ai-title`` lines (the title source).
        chat_records: list[dict[str, object]] = []
        for record, _before, _after in iter_jsonl(path, offset_bytes):
            rtype = record.get("type")
            if rtype in ("assistant", "user", "system"):
                chat_records.append(record)
            elif rtype == "ai-title":
                candidate = opt_str(record.get("aiTitle"))
                if candidate:
                    # ``ai-title`` lines are interleaved arbitrarily; the
                    # LAST one wins per the spec.
                    title = candidate

        # Parent-chain depth per upstream uuid (stable, file-position-free
        # tie-break when timestamps coincide).
        depth = _parent_depth_map(chat_records)

        # Sort by (timestamp, depth, file-order).  file-order is the implicit
        # third key — Python's sort is stable, so records with equal
        # timestamp + depth preserve their original file order.
        def _sort_key(rec: dict[str, object]) -> tuple[str, int, int]:
            ts = opt_str(rec.get("timestamp")) or ""
            uuid = opt_str(rec.get("uuid")) or ""
            return (ts, depth.get(uuid, 0), 0)

        sorted_chats = sorted(chat_records, key=_sort_key)

        seq = next_seq
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        first_cwd: str | None = None
        first_user_text_snippet: str | None = None
        external_id: str | None = session.external_id if session is not None else None
        session_id: str | None = session.id if session is not None else None
        prefix: str | None = (
            f"{SourceKind.CLAUDE_JSONL.value}:{external_id}" if external_id else None
        )

        for rec in sorted_chats:
            rtype = rec.get("type")
            timestamp = opt_str(rec.get("timestamp"))
            uuid = opt_str(rec.get("uuid")) or ""
            parent_uuid_raw = rec.get("parentUuid")
            parent_uuid: str | None = (
                parent_uuid_raw if isinstance(parent_uuid_raw, str) and parent_uuid_raw else None
            )
            session_id_raw = opt_str(rec.get("sessionId")) or ""
            cwd = opt_str(rec.get("cwd"))

            if timestamp is not None:
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
            if first_cwd is None and cwd:
                first_cwd = cwd
            if external_id is None and session_id_raw:
                external_id = session_id_raw
                prefix = f"{SourceKind.CLAUDE_JSONL.value}:{external_id}"
            if session_id is None and external_id is not None:
                session_id = f"{SourceKind.CLAUDE_JSONL.value}:{external_id}"

            if rtype == "system":
                text = _message_text_from_obj(rec.get("content", ""))
                if uuid and prefix and session_id is not None:
                    messages.append(
                        HistoryMessageRecord(
                            id=f"{prefix}:msg:{uuid}",
                            session_id=session_id,
                            external_id=uuid,
                            role="system",
                            parent_id=parent_uuid,
                            seq=seq,
                            text=text,
                            created_at=timestamp,
                        )
                    )
                    seq += 1
                continue

            if rtype == "user":
                if rec.get("isMeta") is True:
                    continue
                message = rec.get("message")
                if not isinstance(message, dict):
                    continue
                role = "user"
                text = _message_text_from_obj(message.get("content", []))
                if first_user_text_snippet is None and text.strip():
                    snippet = text.strip().splitlines()[0]
                    first_user_text_snippet = snippet[:200]
                if uuid and prefix and session_id is not None:
                    messages.append(
                        HistoryMessageRecord(
                            id=f"{prefix}:msg:{uuid}",
                            session_id=session_id,
                            external_id=uuid,
                            role=role,
                            parent_id=parent_uuid,
                            seq=seq,
                            text=text,
                            created_at=timestamp,
                        )
                    )
                    seq += 1
                content = message.get("content", [])
                if isinstance(content, list) and prefix is not None and session_id is not None:
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        anchor = opt_str(block.get("tool_use_id")) or ""
                        body = _stringify_block_content(block.get("content"))
                        is_error = bool(block.get("is_error"))
                        if not anchor:
                            continue
                        events.append(
                            HistoryEventRecord(
                                id=f"{prefix}:evt:{anchor}:{HistoryEventKind.TOOL_RESULT.value}",
                                session_id=session_id,
                                message_id=(f"{prefix}:msg:{uuid}" if uuid else None),
                                message_external_id=uuid,
                                anchor=anchor,
                                kind=HistoryEventKind.TOOL_RESULT,
                                tool_name=None,
                                file_path=None,
                                ordinal=0,
                                body=body,
                                is_error=is_error,
                                created_at=timestamp,
                            )
                        )
                continue

            if rtype == "assistant":
                message = rec.get("message")
                if not isinstance(message, dict):
                    continue
                role = opt_str(message.get("role")) or "assistant"
                content = message.get("content", [])
                text = (
                    _message_text_from_obj(content)
                    if isinstance(content, list)
                    else (content if isinstance(content, str) else "")
                )
                if uuid and prefix and session_id is not None:
                    messages.append(
                        HistoryMessageRecord(
                            id=f"{prefix}:msg:{uuid}",
                            session_id=session_id,
                            external_id=uuid,
                            role=role,
                            parent_id=parent_uuid,
                            seq=seq,
                            text=text,
                            created_at=timestamp,
                        )
                    )
                    seq += 1
                if isinstance(content, list) and prefix is not None and session_id is not None:
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            thinking = opt_str(block.get("thinking")) or ""
                            anchor_uuid = opt_str(block.get("id")) or uuid or ""
                            if not anchor_uuid:
                                continue
                            events.append(
                                HistoryEventRecord(
                                    id=(
                                        f"{prefix}:evt:{anchor_uuid}:"
                                        f"{HistoryEventKind.REASONING.value}"
                                    ),
                                    session_id=session_id,
                                    message_id=(f"{prefix}:msg:{uuid}" if uuid else None),
                                    message_external_id=uuid,
                                    anchor=anchor_uuid,
                                    kind=HistoryEventKind.REASONING,
                                    tool_name=None,
                                    file_path=None,
                                    ordinal=0,
                                    body=thinking,
                                    is_error=False,
                                    created_at=timestamp,
                                )
                            )
                            continue
                        if btype == "tool_use":
                            anchor = opt_str(block.get("id")) or ""
                            tool_name = opt_str(block.get("name"))
                            tool_input = block.get("input")
                            body = json.dumps(tool_input, ensure_ascii=False)
                            file_path = _tool_input_file_path(tool_name, tool_input)
                            if not anchor:
                                continue
                            events.append(
                                HistoryEventRecord(
                                    id=(
                                        f"{prefix}:evt:{anchor}:{HistoryEventKind.TOOL_CALL.value}"
                                    ),
                                    session_id=session_id,
                                    message_id=(f"{prefix}:msg:{uuid}" if uuid else None),
                                    message_external_id=uuid,
                                    anchor=anchor,
                                    kind=HistoryEventKind.TOOL_CALL,
                                    tool_name=tool_name,
                                    file_path=file_path,
                                    ordinal=0,
                                    body=body,
                                    is_error=False,
                                    created_at=timestamp,
                                )
                            )

        if session is not None:
            # Resume: keep the handed-in session's identity and project.
            final_session = session
            if last_timestamp and (session.ended_at is None or last_timestamp > session.ended_at):
                final_session = final_session.model_copy(
                    update={"ended_at": last_timestamp},
                )
        else:
            if external_id is None or session_id is None:
                msg = f"{path}: no chat lines found and no session supplied"
                raise ValueError(msg)
            inferred_project = PurePosixPath(first_cwd).name if first_cwd else ""
            final_title = title or first_user_text_snippet or ""
            final_session = HistorySessionRecord(
                id=session_id,
                external_id=external_id,
                project=project or inferred_project,
                title=final_title,
                started_at=first_timestamp,
                ended_at=last_timestamp,
                source_kind=SourceKind.CLAUDE_JSONL,
                source_ref=str(path),
            )

        # Use the real end offset of fully parsed lines so append-growth
        # resumes cleanly.
        end_offset = offset_bytes
        for _rec, _before, after in iter_jsonl(path, offset_bytes):
            end_offset = after

        return HistoryImportBatch(
            session=final_session,
            messages=messages,
            events=events,
            next_offset_bytes=end_offset,
            next_seq=seq,
        )
