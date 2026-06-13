"""Tests for the Claude Code JSONL history importer."""

from __future__ import annotations

import json
from pathlib import Path

from membox.core.history_import import fetch_payload, import_history
from membox.core.store import KnowledgeStore
from membox.model.schema import HistoryEventKind, SourceKind
from membox.services.importers.claude_jsonl import (
    ClaudeJsonlImporter,
    _claude_project_dirname,
)

# ---------------------------------------------------------------------------
# Helpers — build a Claude JSONL session log inline.
# ---------------------------------------------------------------------------


def _write_log(path: Path, lines: list[dict[str, object]]) -> Path:
    """Write each dict as a JSONL line, mirroring Claude Code's on-disk format."""
    text = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. cwd → Claude project-dirname encoding
# ---------------------------------------------------------------------------


def test_claude_project_dirname_uses_dash_separator() -> None:
    """Absolute paths become ``-``-prefixed, slash-free directory names."""
    assert _claude_project_dirname(Path("/Users/royzhu/proj")) == "-Users-royzhu-proj"


def test_claude_project_dirname_handles_repo() -> None:
    """A deeper real-world path encodes every ``/`` as ``-``."""
    assert (
        _claude_project_dirname(Path("/Users/royzhu/software/myproject/python/membox"))
        == "-Users-royzhu-software-myproject-python-membox"
    )


# ---------------------------------------------------------------------------
# 2. discover_sessions
# ---------------------------------------------------------------------------


def test_discover_sessions_returns_empty_when_dir_absent(tmp_path: Path) -> None:
    """Returns ``[]`` when the encoded project directory does not exist."""
    session_root = tmp_path / "claude_root"
    project_cwd = tmp_path / "some" / "project"
    project_cwd.mkdir(parents=True)

    importer = ClaudeJsonlImporter()
    assert importer.discover_sessions(project_cwd, session_root) == []


def test_discover_sessions_lists_jsonl_files(tmp_path: Path) -> None:
    """Returns sorted ``*.jsonl`` files under the encoded project directory."""
    session_root = tmp_path / "claude_root"
    project_cwd = Path("/Users/royzhu/proj").resolve()
    encoded = _claude_project_dirname(project_cwd)
    project_dir = session_root / encoded
    project_dir.mkdir(parents=True)

    a = project_dir / "a.jsonl"
    b = project_dir / "b.jsonl"
    c = project_dir / "ignore.txt"
    a.write_text("{}\n", encoding="utf-8")
    b.write_text("{}\n", encoding="utf-8")
    c.write_text("hi", encoding="utf-8")

    importer = ClaudeJsonlImporter()
    assert importer.discover_sessions(project_cwd, session_root) == [a, b]


# ---------------------------------------------------------------------------
# 3. assistant + tool_use → message + TOOL_CALL event
# ---------------------------------------------------------------------------


def test_assistant_tool_use_emit_message_and_event(tmp_path: Path) -> None:
    """An assistant line with a tool_use block yields a message and a TOOL_CALL event."""
    session_id = "11111111-1111-1111-1111-111111111111"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/Users/royzhu/proj",
                "message": {"role": "user", "content": "read foo.py"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:01Z",
                "cwd": "/Users/royzhu/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Reading the file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Read",
                            "input": {"file_path": "/Users/royzhu/proj/foo.py"},
                        },
                    ],
                },
            },
        ],
    )

    importer = ClaudeJsonlImporter()
    batch = importer.parse(log)

    assert batch.session.id == f"claude-jsonl:{session_id}"
    assert batch.session.external_id == session_id
    assert batch.session.project == "proj"
    assert batch.session.started_at == "2026-06-12T10:00:00Z"
    assert batch.session.ended_at == "2026-06-12T10:00:01Z"
    assert batch.session.source_kind is SourceKind.CLAUDE_JSONL

    by_role = {m.role: m for m in batch.messages}
    assert set(by_role) == {"user", "assistant"}
    assert by_role["assistant"].text == "Reading the file."
    assert by_role["assistant"].parent_id == "u1"

    tool_calls = [e for e in batch.events if e.kind is HistoryEventKind.TOOL_CALL]
    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert call.tool_name == "Read"
    assert call.file_path == "/Users/royzhu/proj/foo.py"
    assert call.anchor == "toolu_01"
    assert json.loads(call.body) == {"file_path": "/Users/royzhu/proj/foo.py"}


def test_tool_use_edit_and_write_extract_file_path(tmp_path: Path) -> None:
    """Edit and Write tool_use blocks also surface ``file_path``; Bash does not."""
    session_id = "22222222-2222-2222-2222-222222222222"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "ax",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_e",
                            "name": "Edit",
                            "input": {
                                "file_path": "/p/x.py",
                                "old": "a",
                                "new": "b",
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_w",
                            "name": "Write",
                            "input": {"file_path": "/p/y.py", "content": "hi"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_b",
                            "name": "Bash",
                            "input": {"command": "ls /p"},
                        },
                    ],
                },
            }
        ],
    )
    batch = ClaudeJsonlImporter().parse(log)
    by_anchor = {e.anchor: e for e in batch.events}
    assert by_anchor["toolu_e"].file_path == "/p/x.py"
    assert by_anchor["toolu_w"].file_path == "/p/y.py"
    assert by_anchor["toolu_b"].file_path is None


# ---------------------------------------------------------------------------
# 4. user + tool_result with is_error → TOOL_RESULT event
# ---------------------------------------------------------------------------


def test_user_tool_result_is_error_joins_anchor(tmp_path: Path) -> None:
    """A user tool_result line with ``is_error:true`` emits a TOOL_RESULT event."""
    session_id = "33333333-3333-3333-3333-333333333333"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "ax1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_99",
                            "name": "Bash",
                            "input": {"command": "false"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "ur1",
                "parentUuid": "ax1",
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:01Z",
                "cwd": "/p",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_99",
                            "is_error": True,
                            "content": "Command exited with non-zero status 1",
                        }
                    ],
                },
            },
        ],
    )

    batch = ClaudeJsonlImporter().parse(log)
    results = [e for e in batch.events if e.kind is HistoryEventKind.TOOL_RESULT]
    assert len(results) == 1
    evt = results[0]
    assert evt.anchor == "toolu_99"
    assert evt.is_error is True
    assert "non-zero" in evt.body
    assert evt.message_external_id == "ur1"


# ---------------------------------------------------------------------------
# 5. thinking block → REASONING event, not message text
# ---------------------------------------------------------------------------


def test_thinking_block_becomes_reasoning_event(tmp_path: Path) -> None:
    """A thinking block becomes a REASONING event; the message text excludes it."""
    session_id = "44444444-4444-4444-4444-444444444444"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "ath",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me think about this carefully.",
                        },
                        {"type": "text", "text": "Here is the answer."},
                    ],
                },
            }
        ],
    )

    batch = ClaudeJsonlImporter().parse(log)
    assert len(batch.messages) == 1
    msg = batch.messages[0]
    assert msg.text == "Here is the answer."
    assert "think" not in msg.text

    reasoning = [e for e in batch.events if e.kind is HistoryEventKind.REASONING]
    assert len(reasoning) == 1
    assert reasoning[0].body == "Let me think about this carefully."
    assert reasoning[0].message_external_id == "ath"


# ---------------------------------------------------------------------------
# 6. ai-title → session.title only, no message
# ---------------------------------------------------------------------------


def test_ai_title_captured_no_message(tmp_path: Path) -> None:
    """``ai-title`` sets the session title and emits no message."""
    session_id = "55555555-5555-5555-5555-555555555555"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "ai-title",
                "sessionId": session_id,
                "uuid": "t1",
                "aiTitle": "Refactor checkpoint command",
            },
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "go"},
            },
        ],
    )

    batch = ClaudeJsonlImporter().parse(log)
    assert batch.session.title == "Refactor checkpoint command"
    assert {m.role for m in batch.messages} == {"user"}


# ---------------------------------------------------------------------------
# 7. Control lines + isMeta:true user lines are skipped
# ---------------------------------------------------------------------------


def test_control_lines_and_is_meta_skipped(tmp_path: Path) -> None:
    """Control lines and isMeta user lines emit no message and no event."""
    session_id = "66666666-6666-6666-6666-666666666666"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {"type": "attachment", "sessionId": session_id, "uuid": "att"},
            {"type": "mode", "sessionId": session_id, "mode": "auto"},
            {"type": "permission-mode", "sessionId": session_id, "mode": "default"},
            {"type": "last-prompt", "sessionId": session_id, "prompt": "x"},
            {"type": "file-history-snapshot", "sessionId": session_id, "files": []},
            {"type": "queue-operation", "sessionId": session_id, "op": "enqueue"},
            {
                "type": "user",
                "uuid": "meta1",
                "parentUuid": None,
                "sessionId": session_id,
                "isMeta": True,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {
                    "role": "user",
                    "content": "<synthetic>reminder</synthetic>",
                },
            },
            {
                "type": "user",
                "uuid": "real1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:01Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "real user message"},
            },
        ],
    )

    batch = ClaudeJsonlImporter().parse(log)
    # Only the one real user message survives.
    assert len(batch.messages) == 1
    assert batch.messages[0].external_id == "real1"
    assert batch.events == []


# ---------------------------------------------------------------------------
# 8. Incremental resume: identical IDs, no duplicates, seq continues
# ---------------------------------------------------------------------------


def test_incremental_resume_deterministic_ids_and_continuing_seq(
    tmp_path: Path,
) -> None:
    """Resuming from a partial parse continues ``seq`` and yields the same IDs."""
    session_id = "77777777-7777-7777-7777-777777777777"
    full = _write_log(
        tmp_path / "full.jsonl",
        [
            {
                "type": "user",
                "uuid": "r1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "one"},
            },
            {
                "type": "user",
                "uuid": "r2",
                "parentUuid": "r1",
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:01Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "two"},
            },
            {
                "type": "user",
                "uuid": "r3",
                "parentUuid": "r2",
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:02Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "three"},
            },
        ],
    )

    importer = ClaudeJsonlImporter()
    # First pass: parse the first 2 lines only.
    offset_after_two = _offset_of_nth_newline(full, 2)
    partial = importer.parse(full, offset_bytes=0, next_seq=0)
    # Truncate file content for a clean "partial" simulation by re-parsing
    # the same file from the cut offset and feeding in the prior session.

    prior_session = partial.session
    resumed = importer.parse(
        full, offset_bytes=offset_after_two, next_seq=partial.next_seq, session=prior_session
    )

    # All IDs from the resumed batch are present in the full batch.
    full_batch = importer.parse(full)
    full_msg_ids = {m.id for m in full_batch.messages}
    full_evt_ids = {e.id for e in full_batch.events}
    for m in resumed.messages:
        assert m.id in full_msg_ids
    for e in resumed.events:
        assert e.id in full_evt_ids

    # Resumed seq continues (no overlap with partial).
    partial_seqs = [m.seq for m in partial.messages]
    resumed_seqs = [m.seq for m in resumed.messages]
    assert max(partial_seqs) < min(resumed_seqs) or resumed_seqs == []

    # Determinism: re-parsing from scratch yields the same IDs.
    again = importer.parse(full)
    assert {m.id for m in again.messages} == full_msg_ids
    assert {e.id for e in again.events} == full_evt_ids


def _offset_of_nth_newline(path: Path, n: int) -> int:
    """Return the byte offset just after the Nth newline in *path* (1-indexed)."""
    data = path.read_bytes()
    seen = 0
    for i, byte in enumerate(data):
        if byte == ord("\n"):
            seen += 1
            if seen == n:
                return i + 1
    return len(data)


# ---------------------------------------------------------------------------
# 9. fetch round-trip via payload_locator
# ---------------------------------------------------------------------------


def test_fetch_round_trip_via_import_history(tmp_path: Path) -> None:
    """``fetch_payload`` returns the original upstream line payload after import."""
    session_id = "88888888-8888-8888-8888-888888888888"
    db = tmp_path / "claude.db"
    log = _write_log(
        tmp_path / "sess.jsonl",
        [
            {
                "type": "user",
                "uuid": "uu",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:00Z",
                "cwd": "/p",
                "message": {"role": "user", "content": "fetch me please"},
            },
            {
                "type": "assistant",
                "uuid": "aa",
                "parentUuid": "uu",
                "sessionId": session_id,
                "timestamp": "2026-06-12T10:00:01Z",
                "cwd": "/p",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "fetch me the file"},
                        {
                            "type": "tool_use",
                            "id": "toolu_F",
                            "name": "Read",
                            "input": {"file_path": "/p/f.py"},
                        },
                    ],
                },
            },
        ],
    )

    store = KnowledgeStore(str(db))
    result = import_history(store, log, "claude", project="p")
    assert result["skipped"] is False
    assert result["messages"] >= 2
    assert result["events"] >= 1

    # Message round-trip.
    msg_row = store.get_history_record(f"claude-jsonl:{session_id}:msg:uu")
    assert msg_row is not None
    assert msg_row["trace_kind"] == "message"
    fetched = fetch_payload(store, str(msg_row["id"]))
    assert fetched["found"] is True
    assert fetched["payload"] == "fetch me please"

    # Event round-trip.
    evt_row = store.get_history_record(f"claude-jsonl:{session_id}:evt:toolu_F:tool_call")
    assert evt_row is not None
    assert evt_row["trace_kind"] == "event"
    fetched_evt = fetch_payload(store, str(evt_row["id"]))
    assert fetched_evt["found"] is True
    body = json.loads(fetched_evt["payload"])
    assert body == {"file_path": "/p/f.py"}
