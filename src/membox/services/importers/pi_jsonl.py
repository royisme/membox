"""Importer for Pi coding agent session logs (``pi`` adapt format).

Pi stores one session per file under
``~/.pi/agent/sessions/<project-dir>/<timestamp>_<uuid>.jsonl``.

This adapter extracts **conversation only** — user and assistant text messages.
Tool calls, tool results, thinking, model changes, and other non-conversation
events are skipped.  The goal is agent memory from dialogue, not from tool
execution logs.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from membox.model.schema import (
    HistoryImportBatch,
    HistoryMessageRecord,
    HistorySessionRecord,
    SourceKind,
)
from membox.services.importers.common import iter_jsonl, opt_str


def _message_text_from_obj(content: object) -> str:
    """Extract text from either a string or Pi content-part array (``text`` parts only)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


class PiJsonlImporter:
    """Parses Pi coding agent session logs — conversation only."""

    format_name = "pi"

    # -- session discovery ------------------------------------------------

    @staticmethod
    def _peek_session_cwd(path: Path) -> str | None:
        """Read ``cwd`` from the first ``session`` record without full parse."""
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("type") == "session":
                        cwd = record.get("cwd")
                        return cwd if isinstance(cwd, str) else None
        except OSError:
            return None
        return None

    def discover_sessions(self, project_cwd: Path, session_root: Path) -> list[Path]:
        """Return Pi session ``.jsonl`` files matching *project_cwd* under *session_root*.

        Scans *session_root* subdirectories, peeks at session ``cwd`` headers,
        and returns files whose recorded cwd matches *project_cwd*.
        """
        root = session_root.expanduser().resolve()
        if not root.is_dir():
            return []
        resolved_target = project_cwd.resolve()
        results: list[Path] = []
        for session_dir in sorted(root.iterdir()):
            if not session_dir.is_dir():
                continue
            files = sorted(session_dir.glob("*.jsonl"))
            if not files:
                continue
            peek_cwd = self._peek_session_cwd(files[0])
            if peek_cwd is None:
                continue
            try:
                if Path(peek_cwd).resolve() == resolved_target:
                    results.extend(files)
            except OSError:
                continue
        return sorted(results, reverse=True)

    # -- parse ------------------------------------------------------------

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse one Pi session log — conversation messages only.

        User and assistant text is extracted; tool calls, tool results,
        thinking, model changes, and custom events are skipped.
        """
        messages: list[HistoryMessageRecord] = []
        seq = next_seq
        end_offset = offset_bytes
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

            # Only conversation messages.
            if rtype != "message":
                continue
            msg = record.get("message")
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", ""))
            if role not in ("user", "assistant"):
                continue

            text = _message_text_from_obj(msg.get("content", []))
            if not text.strip():
                continue

            pi_msg_id = str(record.get("id", ""))
            created = opt_str(msg.get("timestamp"))
            messages.append(
                HistoryMessageRecord(
                    id=f"{SourceKind.PI_JSONL.value}:{session.external_id}:msg:{pi_msg_id}",
                    session_id=session.id,
                    external_id=pi_msg_id,
                    role=role,
                    seq=seq,
                    text=text,
                    created_at=created or timestamp,
                )
            )
            seq += 1

        if session is None:
            msg = f"{path}: no session record found"
            raise ValueError(msg)
        if session.ended_at is None and last_timestamp:
            session = session.model_copy(update={"ended_at": last_timestamp})
        return HistoryImportBatch(
            session=session,
            messages=messages,
            events=[],
            next_offset_bytes=end_offset,
            next_seq=seq,
        )
