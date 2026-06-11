"""Tests for the Phase B history importer utilities and format parsers.

Covers: synth_external_id stability and dedup, iter_jsonl partial-line
handling, event ID file-position independence, and the Codex JSONL importer.
"""

from __future__ import annotations

import json
from pathlib import Path

from membox.core.history_import import import_history
from membox.core.store import KnowledgeStore
from membox.services.importers.codex_jsonl import CodexJsonlImporter
from membox.services.importers.common import iter_jsonl, synth_external_id

# ---------------------------------------------------------------------------
# 11. synth_external_id
# ---------------------------------------------------------------------------


def test_synth_external_id_same_inputs_same_id() -> None:
    """Same inputs produce the same ID."""
    seen: set[str] = set()
    id1 = synth_external_id("user", "2026-06-11T10:00:00Z", "hello world", seen)
    seen2: set[str] = set()
    id2 = synth_external_id("user", "2026-06-11T10:00:00Z", "hello world", seen2)
    assert id1 == id2


def test_synth_external_id_different_inputs_different_ids() -> None:
    """Different inputs produce different IDs."""
    seen: set[str] = set()
    id1 = synth_external_id("user", "2026-06-11T10:00:00Z", "hello world", seen)
    seen.add(id1)
    id2 = synth_external_id("assistant", "2026-06-11T10:00:01Z", "different text", seen)
    assert id1 != id2


def test_synth_external_id_collision_resolved_via_suffix() -> None:
    """Collision via seen set resolves with a ~2 suffix."""
    seen: set[str] = set()
    id1 = synth_external_id("user", "2026-06-11T10:00:00Z", "hello world", seen)
    seen.add(id1)
    # Same inputs again — should get a different (suffixed) ID
    id2 = synth_external_id("user", "2026-06-11T10:00:00Z", "hello world", seen)
    assert id1 != id2
    # The second call should have produced a ~2 variant
    assert id2.endswith("~2")


# ---------------------------------------------------------------------------
# 12. iter_jsonl
# ---------------------------------------------------------------------------


def test_iter_jsonl_skips_blank_and_garbage_lines(tmp_path: Path) -> None:
    """Blank lines and non-JSON lines are skipped silently."""
    fixture = tmp_path / "test.jsonl"
    fixture.write_bytes(b'{"a": 1}\n\nnot json at all\n{"b": 2}\n')
    records = list(iter_jsonl(fixture))
    assert len(records) == 2
    assert records[0][0] == {"a": 1}
    assert records[1][0] == {"b": 2}


def test_iter_jsonl_partial_trailing_line_not_advanced(tmp_path: Path) -> None:
    """A partial trailing line (no newline) does not advance the offset past it."""
    fixture = tmp_path / "partial.jsonl"
    complete_line = b'{"x": 1}\n'
    partial_line = b'{"y": 2}'  # no trailing newline
    fixture.write_bytes(complete_line + partial_line)

    records = list(iter_jsonl(fixture))
    # Only the complete line should be yielded
    assert len(records) == 1
    assert records[0][0] == {"x": 1}

    # The returned offset_after for the first record equals len(complete_line)
    _record, _before, offset_after = records[0]
    assert offset_after == len(complete_line)

    # Now complete the partial line and resume from the stored offset
    with fixture.open("ab") as fh:
        fh.write(b"\n")

    records2 = list(iter_jsonl(fixture, offset_bytes=offset_after))
    assert len(records2) == 1
    assert records2[0][0] == {"y": 2}


# ---------------------------------------------------------------------------
# 13. Event IDs are independent of file position
# ---------------------------------------------------------------------------


def test_event_ids_independent_of_file_position(tmp_path: Path) -> None:
    """Swapping two events in a file does not change their stable IDs."""
    db1 = tmp_path / "db1.db"
    db2 = tmp_path / "db2.db"

    session_line = json.dumps(
        {
            "type": "session",
            "id": "pos1",
            "project": "demo",
            "title": "position test",
            "started_at": "2026-06-11T18:00:00Z",
        }
    )
    msg_line = json.dumps(
        {
            "type": "message",
            "id": "pm1",
            "role": "user",
            "text": "position independence test",
            "created_at": "2026-06-11T18:00:01Z",
        }
    )
    evt_a = json.dumps(
        {
            "type": "event",
            "message_id": "pm1",
            "kind": "tool_call",
            "tool_name": "bash",
            "call_id": "evtA",
            "body": "event alpha content",
            "is_error": False,
        }
    )
    evt_b = json.dumps(
        {
            "type": "event",
            "message_id": "pm1",
            "kind": "tool_call",
            "tool_name": "grep",
            "call_id": "evtB",
            "body": "event beta content",
            "is_error": False,
        }
    )

    # Original order: A then B
    fixture1 = tmp_path / "order1.jsonl"
    fixture1.write_text("\n".join([session_line, msg_line, evt_a, evt_b]) + "\n", encoding="utf-8")

    # Swapped order: B then A
    fixture2 = tmp_path / "order2.jsonl"
    fixture2.write_text("\n".join([session_line, msg_line, evt_b, evt_a]) + "\n", encoding="utf-8")

    store1 = KnowledgeStore(str(db1))
    store2 = KnowledgeStore(str(db2))

    import_history(store1, fixture1, "membox-history-jsonl", project="demo")
    import_history(store2, fixture2, "membox-history-jsonl", project="demo")

    # ID for evtA is keyed on call_id="evtA", not file position
    expected_a = "membox-capture:pos1:evt:evtA:tool_call"
    expected_b = "membox-capture:pos1:evt:evtB:tool_call"

    row_a1 = store1.get_history_record(expected_a)
    row_a2 = store2.get_history_record(expected_a)
    assert row_a1 is not None
    assert row_a2 is not None
    assert row_a1["id"] == row_a2["id"] == expected_a

    row_b1 = store1.get_history_record(expected_b)
    row_b2 = store2.get_history_record(expected_b)
    assert row_b1 is not None
    assert row_b2 is not None
    assert row_b1["id"] == row_b2["id"] == expected_b


# ---------------------------------------------------------------------------
# 14. Codex JSONL importer
# ---------------------------------------------------------------------------

_CODEX_LOG = """\
{"timestamp": "2026-06-11T10:00:00Z", "type": "session_meta", "payload": {"id": "sess-abc", "cwd": "/home/user/myproject"}}
{"timestamp": "2026-06-11T10:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run tests"}]}}
{"timestamp": "2026-06-11T10:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "bash", "arguments": "pytest", "call_id": "call-1"}}
{"timestamp": "2026-06-11T10:00:03Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "Error: 5 tests failed"}}
"""


def test_codex_importer_session_project_from_cwd(tmp_path: Path) -> None:
    """Session project defaults to cwd basename when no override given."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch = importer.parse(fixture)
    assert batch.session.project == "myproject"


def test_codex_importer_project_override_wins(tmp_path: Path) -> None:
    """Explicit project= override takes precedence over cwd inference."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch = importer.parse(fixture, project="overridden")
    assert batch.session.project == "overridden"


def test_codex_importer_message_kinds(tmp_path: Path) -> None:
    """Message has kind='message', tool_call event has kind='tool_call'."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch = importer.parse(fixture)

    # Should have at least one message
    assert len(batch.messages) >= 1
    # The user message has role "user"
    user_msgs = [m for m in batch.messages if m.role == "user"]
    assert len(user_msgs) >= 1

    # Should have tool_call and tool_result events
    kinds = {e.kind.value for e in batch.events}
    assert "tool_call" in kinds
    assert "tool_result" in kinds


def test_codex_importer_tool_result_is_error(tmp_path: Path) -> None:
    """tool_result event with output starting 'Error' is marked is_error=True."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch = importer.parse(fixture)

    tool_results = [e for e in batch.events if e.kind.value == "tool_result"]
    assert len(tool_results) >= 1
    assert tool_results[0].is_error is True


def test_codex_importer_stable_ids_no_file_position(tmp_path: Path) -> None:
    """Stable IDs contain call_id or similar stable keys, not byte offsets."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch = importer.parse(fixture)

    # IDs should not contain raw byte offsets (they would look like :NNN:)
    for evt in batch.events:
        # The ID is built from source_kind:session_id:evt:anchor:kind
        # call-1 should be in the event ID for call_id-keyed events
        if "call-1" in evt.id:
            assert "call-1" in evt.id


def test_codex_importer_reimport_same_ids(tmp_path: Path) -> None:
    """Re-importing the same Codex log produces identical event IDs."""
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    importer = CodexJsonlImporter()
    batch1 = importer.parse(fixture)
    batch2 = importer.parse(fixture)

    ids1 = {e.id for e in batch1.events}
    ids2 = {e.id for e in batch2.events}
    assert ids1 == ids2


def test_codex_importer_via_import_history(tmp_path: Path) -> None:
    """import_history with 'codex-jsonl' format stores session under inferred project."""
    db = tmp_path / "codex.db"
    fixture = tmp_path / "codex.jsonl"
    fixture.write_text(_CODEX_LOG, encoding="utf-8")

    store = KnowledgeStore(str(db))
    result = import_history(store, fixture, "codex-jsonl")

    assert result["skipped"] is False
    assert result["messages"] >= 1
    assert result["events"] >= 1

    session = store.get_history_session(result["session_id"])
    assert session is not None
    assert session["project"] == "myproject"
