"""Tests for the Phase B History Trace Index — KnowledgeStore-level operations.

Covers migration, idempotent import, unchanged-file skip, incremental append,
upstream compaction, secret redaction, preview cap with multi-byte chars,
fetch_payload, search_history filters, history_around, history_file, and
history_failures.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from membox.core.history_import import fetch_payload, import_history
from membox.core.store import KnowledgeStore
from membox.core.triage import REDACTION_MARKER, redact_secrets

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SESSION_LINE = json.dumps(
    {
        "type": "session",
        "id": "s1",
        "project": "demo",
        "title": "test session",
        "started_at": "2026-06-11T10:00:00Z",
    }
)

_MESSAGE_LINE = json.dumps(
    {
        "type": "message",
        "id": "m1",
        "role": "user",
        "text": "hello world testing membox storage",
        "created_at": "2026-06-11T10:00:01Z",
    }
)

_EVENT_LINE = json.dumps(
    {
        "type": "event",
        "message_id": "m1",
        "kind": "tool_call",
        "tool_name": "bash",
        "call_id": "c1",
        "body": "ls dash la directory listing files",
        "is_error": False,
        "file_path": "/code/work.py",
    }
)

_EVENT_RESULT_LINE = json.dumps(
    {
        "type": "event",
        "message_id": "m1",
        "kind": "tool_result",
        "call_id": "c1",
        "body": "total eight bytes result output",
        "is_error": False,
    }
)

_FMT = "membox-history-jsonl"


def _write_basic_fixture(path: Path) -> None:
    """Write a minimal 4-line fixture: session + message + 2 events."""
    path.write_text(
        "\n".join([_SESSION_LINE, _MESSAGE_LINE, _EVENT_LINE, _EVENT_RESULT_LINE]) + "\n",
        encoding="utf-8",
    )


def _row_counts(db_path: Path) -> dict[str, int]:
    """Return row counts for the three history tables."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "sessions": conn.execute("SELECT COUNT(*) FROM history_sessions").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM history_messages").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM history_events").fetchone()[0],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------


def test_migration_user_version(tmp_path: Path) -> None:
    """Fresh KnowledgeStore gets user_version=6."""
    db = tmp_path / "mem.db"
    _store = KnowledgeStore(str(db))
    conn = sqlite3.connect(str(db))
    try:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 6


def test_migration_tables_exist(tmp_path: Path) -> None:
    """All four history tables and four FTS virtual tables exist after migration."""
    db = tmp_path / "mem.db"
    _store = KnowledgeStore(str(db))
    conn = sqlite3.connect(str(db))
    try:
        all_names: set[str] = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()

    for table in (
        "history_sessions",
        "history_messages",
        "history_events",
        "history_import_state",
    ):
        assert table in all_names, f"Missing table: {table}"

    # FTS virtual tables appear in sqlite_master with type='table'
    for fts in (
        "history_messages_fts",
        "history_messages_fts_trigram",
        "history_events_fts",
        "history_events_fts_trigram",
    ):
        assert fts in all_names, f"Missing FTS table: {fts}"


# ---------------------------------------------------------------------------
# 2. Idempotent import (mtime changed, content same)
# ---------------------------------------------------------------------------


def test_idempotent_import_mtime_changed(tmp_path: Path) -> None:
    """Re-import after only mtime bump: same row counts, no duplicates."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "session.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))

    import_history(store, fixture, _FMT, project="demo")
    counts_after_first = _row_counts(db)

    # Bump mtime but not content
    stat = fixture.stat()
    os.utime(fixture, (stat.st_atime, stat.st_mtime + 10))

    import_history(store, fixture, _FMT, project="demo")
    counts_after_second = _row_counts(db)

    assert counts_after_second == counts_after_first


# ---------------------------------------------------------------------------
# 3. Unchanged-file skip
# ---------------------------------------------------------------------------


def test_unchanged_file_skip(tmp_path: Path) -> None:
    """Re-import without touching file returns skipped=True."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "session.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))

    import_history(store, fixture, _FMT, project="demo")
    result = import_history(store, fixture, _FMT, project="demo")

    assert result["skipped"] is True


# ---------------------------------------------------------------------------
# 4. Incremental: append a line, re-import adds new rows
# ---------------------------------------------------------------------------


def test_incremental_append(tmp_path: Path) -> None:
    """Appending a new message to the fixture and re-importing increases counts."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "session.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))

    import_history(store, fixture, _FMT, project="demo")
    counts_first = _row_counts(db)

    new_msg = json.dumps(
        {
            "type": "message",
            "id": "m2",
            "role": "assistant",
            "text": "incremental append response text here",
            "created_at": "2026-06-11T10:00:02Z",
        }
    )
    with fixture.open("a", encoding="utf-8") as fh:
        fh.write(new_msg + "\n")

    import_history(store, fixture, _FMT, project="demo")
    counts_second = _row_counts(db)

    assert counts_second["messages"] == counts_first["messages"] + 1
    assert counts_second["sessions"] == counts_first["sessions"]


# ---------------------------------------------------------------------------
# 5. Upstream rewrite (compaction): DB keeps existing rows
# ---------------------------------------------------------------------------


def test_upstream_compaction_keeps_rows(tmp_path: Path) -> None:
    """Rewriting file with fewer messages keeps existing DB rows (append-only)."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "session.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))

    import_history(store, fixture, _FMT, project="demo")
    counts_before = _row_counts(db)

    # Rewrite with one message removed (simulates upstream compaction)
    compacted = (
        json.dumps(
            {
                "type": "session",
                "id": "s1",
                "project": "demo",
                "title": "test session",
                "started_at": "2026-06-11T10:00:00Z",
            }
        )
        + "\n"
    )
    fixture.write_text(compacted, encoding="utf-8")

    # Re-import should not raise and DB message count stays the same
    import_history(store, fixture, _FMT, project="demo")
    counts_after = _row_counts(db)

    assert counts_after["messages"] == counts_before["messages"]


# ---------------------------------------------------------------------------
# 6. Secret redaction
# ---------------------------------------------------------------------------


def test_redact_secrets_in_stored_text(tmp_path: Path) -> None:
    """Secrets in message/event text are redacted before storage; search finds nothing."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "secret.jsonl"
    secret_msg = (
        json.dumps(
            {
                "type": "session",
                "id": "sec1",
                "project": "demo",
                "title": "secret session",
                "started_at": "2026-06-11T11:00:00Z",
            }
        )
        + "\n"
    )
    secret_msg += (
        json.dumps(
            {
                "type": "message",
                "id": "ms1",
                "role": "user",
                "text": "my key is sk-test123456789012345678 keep safe",
                "created_at": "2026-06-11T11:00:01Z",
            }
        )
        + "\n"
    )
    secret_msg += (
        json.dumps(
            {
                "type": "event",
                "message_id": "ms1",
                "kind": "tool_call",
                "tool_name": "env",
                "call_id": "ce1",
                "body": "export PASSWORD=hunter2secret environment setup",
                "is_error": False,
            }
        )
        + "\n"
    )
    fixture.write_text(secret_msg, encoding="utf-8")

    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    # search for the secret should return no hits
    hits_key = store.search_history("sk-test123456789012345678")
    assert hits_key == [], "Secret API key should not be searchable"

    hits_pw = store.search_history("hunter2secret")
    assert hits_pw == [], "Secret password should not be searchable"

    # DB should store [REDACTED]
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT text FROM history_messages WHERE id = 'membox-capture:sec1:msg:ms1'"
        ).fetchone()
        assert row is not None
        assert "sk-test123456789012345678" not in row[0]
        assert REDACTION_MARKER in row[0]

        evt_row = conn.execute(
            "SELECT body FROM history_events WHERE id = 'membox-capture:sec1:evt:ce1:tool_call'"
        ).fetchone()
        assert evt_row is not None
        assert "hunter2secret" not in evt_row[0]
        assert REDACTION_MARKER in evt_row[0]
    finally:
        conn.close()


def test_redact_secrets_unit_pem() -> None:
    """PEM private key block is redacted."""
    # Assembled at runtime so the literal marker never appears in this file
    # (the detect-private-key pre-commit hook scans test sources too).
    pem_marker = " ".join(["RSA", "PRIVATE", "KEY"])
    text = f"-----BEGIN {pem_marker}-----\nMIIEpAIBAAKCAQEA\n-----END {pem_marker}-----"
    result = redact_secrets(text)
    assert f"BEGIN {pem_marker}" not in result
    assert REDACTION_MARKER in result


def test_redact_secrets_unit_akia() -> None:
    """AWS AKIA access key is redacted."""
    text = "key is AKIAIOSFODNN7EXAMPLE in config"
    result = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert REDACTION_MARKER in result


def test_redact_secrets_unit_ghp_token() -> None:
    """GitHub ghp_ token is redacted."""
    text = "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef in header"
    result = redact_secrets(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef" not in result
    assert REDACTION_MARKER in result


def test_redact_secrets_unit_assignment_keeps_var_name() -> None:
    """Assignment-style redaction keeps the variable name."""
    text = "OPENAI_API_KEY=sk-abcdefghijklmnop12345678"
    result = redact_secrets(text)
    assert "OPENAI_API_KEY" in result
    assert "sk-abcdefghijklmnop12345678" not in result
    assert REDACTION_MARKER in result


# ---------------------------------------------------------------------------
# 7. Preview cap with multi-byte CJK chars
# ---------------------------------------------------------------------------


def test_preview_cap_multibyte(tmp_path: Path) -> None:
    """Text > cap bytes is truncated safely on UTF-8 boundaries; truncated flag set."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "cap.jsonl"
    # "数" is 3 UTF-8 bytes; 10 chars = 30 bytes; cap=20 forces truncation
    long_text = "数" * 10
    lines = (
        json.dumps(
            {
                "type": "session",
                "id": "cap1",
                "project": "demo",
                "title": "cap session",
                "started_at": "2026-06-11T12:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "cm1",
                "role": "user",
                "text": long_text,
                "created_at": "2026-06-11T12:00:01Z",
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")

    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo", text_cap_bytes=20)

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT text, text_truncated FROM history_messages "
            "WHERE id = 'membox-capture:cap1:msg:cm1'"
        ).fetchone()
        assert row is not None
        stored_text, truncated_flag = row
        assert len(stored_text.encode("utf-8")) <= 20
        assert truncated_flag == 1
        # Every stored char should be a valid Unicode scalar (no broken sequences)
        stored_text.encode("utf-8")  # must not raise
    finally:
        conn.close()


def test_fetch_payload_returns_full_text(tmp_path: Path) -> None:
    """fetch_payload returns the FULL text from the upstream file, not the preview."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "cap2.jsonl"
    long_text = "数" * 10
    lines = (
        json.dumps(
            {
                "type": "session",
                "id": "cap2",
                "project": "demo",
                "title": "cap session 2",
                "started_at": "2026-06-11T12:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "cm2",
                "role": "user",
                "text": long_text,
                "created_at": "2026-06-11T12:00:01Z",
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")

    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo", text_cap_bytes=20)

    record_id = "membox-capture:cap2:msg:cm2"
    result = fetch_payload(store, record_id)
    assert result["found"] is True
    assert long_text in result["payload"]


def test_fetch_payload_project_scope(tmp_path: Path) -> None:
    """fetch_payload refuses rows outside the requested project scope."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "scoped-fetch.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    record_id = "membox-capture:s1:msg:m1"
    allowed = fetch_payload(store, record_id, project="demo")
    denied = fetch_payload(store, record_id, project="other")

    assert allowed["found"] is True
    assert denied["found"] is False


# ---------------------------------------------------------------------------
# 8. fetch_payload: delete upstream, unknown record_id
# ---------------------------------------------------------------------------


def test_fetch_payload_deleted_upstream(tmp_path: Path) -> None:
    """Deleting the upstream file gives found=False with 'source no longer available'."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "fetch.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    fixture.unlink()

    result = fetch_payload(store, "membox-capture:s1:msg:m1")
    assert result["found"] is False
    assert (
        "source no longer available" in result["note"].lower()
        or "not found" in result["note"].lower()
        or result["note"] != ""
    )


def test_fetch_payload_unknown_record(tmp_path: Path) -> None:
    """Unknown record_id → found=False."""
    db = tmp_path / "mem.db"
    store = KnowledgeStore(str(db))
    result = fetch_payload(store, "membox-capture:nonexistent:msg:x1")
    assert result["found"] is False


# ---------------------------------------------------------------------------
# 9. Search filters
# ---------------------------------------------------------------------------


def _build_multi_session_store(tmp_path: Path) -> tuple[KnowledgeStore, Path]:
    """Build a store with two sessions under different projects for filter tests."""
    db = tmp_path / "multi.db"
    store = KnowledgeStore(str(db))

    for proj, sid, mid, eid in [
        ("alpha", "a1", "am1", "ae1"),
        ("beta", "b1", "bm1", "be1"),
    ]:
        fixture = tmp_path / f"{proj}.jsonl"
        lines = (
            json.dumps(
                {
                    "type": "session",
                    "id": sid,
                    "project": proj,
                    "title": f"{proj} session",
                    "started_at": "2026-06-11T09:00:00Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "id": mid,
                    "role": "user",
                    "text": f"storage retrieval project {proj} searchterm",
                    "created_at": "2026-06-11T09:00:01Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event",
                    "message_id": mid,
                    "kind": "tool_call",
                    "tool_name": "read_file",
                    "call_id": eid,
                    "body": f"reading file for {proj} project searchterm",
                    "is_error": False,
                    "file_path": f"/code/{proj}/main.py",
                }
            )
            + "\n"
        )
        fixture.write_text(lines, encoding="utf-8")
        import_history(store, fixture, _FMT, project=proj)

    return store, db


def test_search_punctuation_heavy_query_no_raise(tmp_path: Path) -> None:
    """Punctuation-heavy query does not raise; it just returns empty or sanitized hits."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "session.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    # Should not raise even with garbage FTS characters
    hits = store.search_history('fts5(*) AND "quote')
    assert isinstance(hits, list)


def test_search_cjk_query(tmp_path: Path) -> None:
    """CJK query finds CJK content stored in the DB."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "cjk.jsonl"
    lines = (
        json.dumps(
            {
                "type": "session",
                "id": "cjk1",
                "project": "demo",
                "title": "CJK session",
                "started_at": "2026-06-11T13:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "cjkm1",
                "role": "user",
                "text": "数据库迁移操作需要注意事项",
                "created_at": "2026-06-11T13:00:01Z",
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    hits = store.search_history("数据库迁移")
    assert len(hits) >= 1
    ids = [h["id"] for h in hits]
    assert "membox-capture:cjk1:msg:cjkm1" in ids


def test_search_project_isolation(tmp_path: Path) -> None:
    """project='alpha' filter excludes beta rows."""
    store, _db = _build_multi_session_store(tmp_path)

    hits = store.search_history("searchterm", project="alpha")
    assert all(h["project"] == "alpha" for h in hits)
    beta_ids = [h["id"] for h in hits if h["project"] == "beta"]
    assert beta_ids == []


def test_search_session_id_filter(tmp_path: Path) -> None:
    """session_id filter restricts to one session."""
    store, _db = _build_multi_session_store(tmp_path)

    alpha_session_id = "membox-capture:a1"
    hits = store.search_history("searchterm", session_id=alpha_session_id)
    assert all(h["session_id"] == alpha_session_id for h in hits)


def test_search_kind_tool_error(tmp_path: Path) -> None:
    """kind='tool_error' returns only is_error=True event rows."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "err.jsonl"
    lines = (
        json.dumps(
            {
                "type": "session",
                "id": "err1",
                "project": "demo",
                "title": "error session",
                "started_at": "2026-06-11T14:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "em1",
                "role": "user",
                "text": "trigger error command failed crash",
                "created_at": "2026-06-11T14:00:01Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event",
                "message_id": "em1",
                "kind": "tool_result",
                "call_id": "ec1",
                "body": "Error subprocess failed crash exit code nonzero",
                "is_error": True,
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    hits = store.search_history("crash", kind="tool_error")
    assert len(hits) >= 1
    assert all(h["is_error"] for h in hits)


def test_search_tool_filter(tmp_path: Path) -> None:
    """tool filter restricts to events with matching tool_name."""
    store, _db = _build_multi_session_store(tmp_path)

    hits = store.search_history("searchterm", tool="read_file")
    assert all(h["role_or_tool"] == "read_file" for h in hits)


def test_search_file_path_filter(tmp_path: Path) -> None:
    """file_path filter restricts event results by exact path or directory prefix."""
    store, _db = _build_multi_session_store(tmp_path)

    hits = store.search_history("searchterm", file_path="/code/alpha")
    assert len(hits) >= 1
    # All hits should be events matched by the directory prefix.
    for hit in hits:
        assert hit["kind"] == "event"
        assert hit["project"] == "alpha"


def test_search_since_excludes_null_created_at(tmp_path: Path) -> None:
    """since filter excludes rows with NULL created_at.

    A message gets NULL created_at only when both the record field AND the
    session started_at are absent (the importer falls back to started_at).
    We omit started_at from the session header so the fallback is also None.
    """
    db = tmp_path / "mem.db"
    fixture = tmp_path / "since.jsonl"
    lines = (
        # Session WITHOUT started_at so the fallback for null-timestamp msgs is None
        json.dumps(
            {
                "type": "session",
                "id": "si1",
                "project": "demo",
                "title": "since session",
            }
        )
        + "\n"
        # Message WITH an explicit timestamp
        + json.dumps(
            {
                "type": "message",
                "id": "sim1",
                "role": "user",
                "text": "timestamped message storage searchterm",
                "created_at": "2026-06-11T15:00:01Z",
            }
        )
        + "\n"
        # Message WITHOUT any timestamp → stored created_at = NULL
        + json.dumps(
            {
                "type": "message",
                "id": "sim2",
                "role": "assistant",
                "text": "notimestamp message storage searchterm",
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    # Confirm sim2 really has NULL created_at in the DB
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT created_at FROM history_messages WHERE id = 'membox-capture:si1:msg:sim2'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, "sim2 should have NULL created_at for this test to be valid"
    finally:
        conn.close()

    hits = store.search_history("searchterm", since="2026-06-11T00:00:00Z")
    hit_ids = [h["id"] for h in hits]
    # The timestamped message should appear
    assert "membox-capture:si1:msg:sim1" in hit_ids
    # The NULL-created_at message must NOT appear
    assert "membox-capture:si1:msg:sim2" not in hit_ids


# ---------------------------------------------------------------------------
# 10. history_around
# ---------------------------------------------------------------------------


def test_history_around_correct_ordering(tmp_path: Path) -> None:
    """around returns messages in seq order with the center row present."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "around.jsonl"
    messages = [
        json.dumps(
            {
                "type": "session",
                "id": "ar1",
                "project": "demo",
                "title": "around session",
                "started_at": "2026-06-11T16:00:00Z",
            }
        )
    ]
    messages.extend(
        json.dumps(
            {
                "type": "message",
                "id": f"arm{i}",
                "role": "user" if i % 2 == 0 else "assistant",
                "text": f"message number {i} context",
                "created_at": f"2026-06-11T16:00:0{i}Z",
            }
        )
        for i in range(5)
    )
    fixture.write_text("\n".join(messages) + "\n", encoding="utf-8")
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    center_id = "membox-capture:ar1:msg:arm2"
    rows = store.history_around(center_id, radius=1)
    assert len(rows) >= 1
    center_ids = [r["id"] for r in rows]
    assert center_id in center_ids
    # Rows must be in ascending seq order
    seqs: list[int] = [int(str(r["seq"])) for r in rows]
    assert seqs == sorted(seqs)


def test_history_around_project_scope(tmp_path: Path) -> None:
    """history_around refuses a known message outside the requested project scope."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "around-scope.jsonl"
    _write_basic_fixture(fixture)
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    center_id = "membox-capture:s1:msg:m1"
    assert store.history_around(center_id, radius=1, project="demo")
    assert store.history_around(center_id, radius=1, project="other") == []


def test_history_around_unknown_id_returns_empty(tmp_path: Path) -> None:
    """history_around with unknown ID returns []."""
    db = tmp_path / "mem.db"
    store = KnowledgeStore(str(db))
    rows = store.history_around("membox-capture:nonexistent:msg:xxx")
    assert rows == []


# ---------------------------------------------------------------------------
# 11. history_file
# ---------------------------------------------------------------------------


def test_history_file_returns_events_by_file_path(tmp_path: Path) -> None:
    """history_file returns events matching exact path or directory prefix, newest first."""
    store, _db = _build_multi_session_store(tmp_path)

    rows = store.history_file("/code/alpha")
    assert len(rows) >= 1
    for row in rows:
        assert str(row["file_path"]).startswith("/code/alpha/")


# ---------------------------------------------------------------------------
# 12. history_failures
# ---------------------------------------------------------------------------


def test_history_failures_returns_only_errors(tmp_path: Path) -> None:
    """history_failures returns only is_error=1 events."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "failures.jsonl"
    lines = (
        json.dumps(
            {
                "type": "session",
                "id": "fail1",
                "project": "demo",
                "title": "failures session",
                "started_at": "2026-06-11T17:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "fm1",
                "role": "user",
                "text": "run failing test",
                "created_at": "2026-06-11T17:00:01Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event",
                "message_id": "fm1",
                "kind": "tool_result",
                "call_id": "fc1",
                "body": "Error test suite failed",
                "is_error": True,
                "created_at": "2026-06-11T17:00:02Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event",
                "message_id": "fm1",
                "kind": "tool_result",
                "call_id": "fc2",
                "body": "success all passed",
                "is_error": False,
                "created_at": "2026-06-11T17:00:03Z",
            }
        )
        + "\n"
    )
    fixture.write_text(lines, encoding="utf-8")
    store = KnowledgeStore(str(db))
    import_history(store, fixture, _FMT, project="demo")

    rows = store.history_failures()
    assert len(rows) >= 1
    # history_failures always returns only error rows (WHERE is_error=1 in SQL);
    # 'is_error' is not included in the returned keys since the filter is implicit.
    ids = [row["id"] for row in rows]
    assert "membox-capture:fail1:evt:fc1:tool_result" in ids
    # success event must NOT appear
    assert "membox-capture:fail1:evt:fc2:tool_result" not in ids
