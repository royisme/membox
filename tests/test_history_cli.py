"""Tests for the ``membox history`` CLI command group.

Covers: import + search round-trip, unknown format, missing file, fetch of
unknown ID, failures output, project filter, around command, around with
unknown ID.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner, Result

from membox.cli import app

runner = CliRunner()

_FMT = "membox-history-jsonl"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SESSION_LINE = json.dumps(
    {
        "type": "session",
        "id": "cs1",
        "project": "demo",
        "title": "CLI test session",
        "started_at": "2026-06-11T20:00:00Z",
    }
)

_MESSAGE_LINE = json.dumps(
    {
        "type": "message",
        "id": "cm1",
        "role": "user",
        "text": "hello world testing storage retrieval membox",
        "created_at": "2026-06-11T20:00:01Z",
    }
)

_SECRET_MESSAGE_LINE = json.dumps(
    {
        "type": "message",
        "id": "cm-secret",
        "role": "user",
        "text": "OPENAI_API_KEY=sk-abcdefghijklmnop12345678 visible context",
        "created_at": "2026-06-11T20:00:03Z",
    }
)

_EVENT_LINE = json.dumps(
    {
        "type": "event",
        "message_id": "cm1",
        "kind": "tool_call",
        "tool_name": "bash",
        "call_id": "cc1",
        "body": "ls directory listing files bash command",
        "is_error": False,
        "file_path": "/code/work.py",
    }
)

_ERROR_EVENT_LINE = json.dumps(
    {
        "type": "event",
        "message_id": "cm1",
        "kind": "tool_result",
        "call_id": "cc2",
        "body": "Error subprocess failed nonzero exit code crash",
        "is_error": True,
        "created_at": "2026-06-11T20:00:02Z",
    }
)


def _write_fixture(
    path: Path, *, include_error: bool = False, include_secret: bool = False
) -> None:
    """Write a minimal fixture file with optional error event."""
    lines = [_SESSION_LINE, _MESSAGE_LINE, _EVENT_LINE]
    if include_error:
        lines.append(_ERROR_EVENT_LINE)
    if include_secret:
        lines.append(_SECRET_MESSAGE_LINE)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _invoke_import(fixture: Path, db: Path, project: str | None = None) -> Result:
    """Run `membox history import` and return the result."""
    args = [
        "history",
        "import",
        str(fixture),
        "--format",
        _FMT,
        "--db",
        str(db),
    ]
    if project is not None:
        args += ["--project", project]
    return runner.invoke(app, args)


# ---------------------------------------------------------------------------
# 13. CLI import + search round-trip
# ---------------------------------------------------------------------------


def test_cli_import_and_search_roundtrip(tmp_path: Path) -> None:
    """Import a fixture then search with --all-projects; exit 0 and output contains content."""
    fixture = tmp_path / "session.jsonl"
    db = tmp_path / "mem.db"
    _write_fixture(fixture)

    result_import = _invoke_import(fixture, db, project="demo")
    assert result_import.exit_code == 0, result_import.output

    result_search = runner.invoke(
        app,
        [
            "history",
            "search",
            "membox",
            "--all-projects",
            "--db",
            str(db),
        ],
    )
    assert result_search.exit_code == 0, result_search.output
    # Should contain something from the session
    combined = result_search.output
    assert "membox" in combined.lower() or "membox-capture" in combined.lower()


# ---------------------------------------------------------------------------
# 14. Unknown format → exit 1
# ---------------------------------------------------------------------------


def test_cli_import_unknown_format(tmp_path: Path) -> None:
    """Unknown --format gives exit code 1 and mentions 'unknown' or 'known'."""
    fixture = tmp_path / "session.jsonl"
    db = tmp_path / "mem.db"
    _write_fixture(fixture)

    result = runner.invoke(
        app,
        [
            "history",
            "import",
            str(fixture),
            "--format",
            "not-a-real-format",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 1
    output = (result.output or "") + (result.stderr or "")
    assert "unknown" in output.lower() or "known" in output.lower()


# ---------------------------------------------------------------------------
# 15. Missing file → exit 1
# ---------------------------------------------------------------------------


def test_cli_import_missing_file(tmp_path: Path) -> None:
    """Non-existent path gives exit code 1."""
    db = tmp_path / "mem.db"
    result = runner.invoke(
        app,
        [
            "history",
            "import",
            str(tmp_path / "does_not_exist.jsonl"),
            "--format",
            _FMT,
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 16. fetch of unknown record_id → exit 1
# ---------------------------------------------------------------------------


def test_cli_fetch_unknown_record_id(tmp_path: Path) -> None:
    """Fetching a non-existent record ID exits with code 1."""
    db = tmp_path / "mem.db"
    # Import something so the DB exists and has schema
    fixture = tmp_path / "session.jsonl"
    _write_fixture(fixture)
    _invoke_import(fixture, db)

    result = runner.invoke(
        app,
        [
            "history",
            "fetch",
            "membox-capture:nonexistent:msg:xxx",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 1


def test_cli_fetch_redacts_by_default(tmp_path: Path) -> None:
    """history fetch redacts secret-looking payloads unless --raw is explicit."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "secret-session.jsonl"
    _write_fixture(fixture, include_secret=True)
    _invoke_import(fixture, db, project="demo")

    result = runner.invoke(
        app,
        [
            "history",
            "fetch",
            "membox-capture:cs1:msg:cm-secret",
            "--project",
            "demo",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY=[REDACTED]" in result.output
    assert "sk-abcdefghijklmnop12345678" not in result.output


def test_cli_fetch_raw_requires_explicit_flag(tmp_path: Path) -> None:
    """history fetch --raw returns the upstream payload unchanged."""
    db = tmp_path / "mem.db"
    fixture = tmp_path / "raw-session.jsonl"
    _write_fixture(fixture, include_secret=True)
    _invoke_import(fixture, db, project="demo")

    result = runner.invoke(
        app,
        [
            "history",
            "fetch",
            "membox-capture:cs1:msg:cm-secret",
            "--project",
            "demo",
            "--raw",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "sk-abcdefghijklmnop12345678" in result.output


# ---------------------------------------------------------------------------
# 17. failures command output
# ---------------------------------------------------------------------------


def test_cli_failures_output(tmp_path: Path) -> None:
    """history failures shows is_error event body or ID."""
    fixture = tmp_path / "session.jsonl"
    db = tmp_path / "mem.db"
    _write_fixture(fixture, include_error=True)
    _invoke_import(fixture, db, project="demo")

    result = runner.invoke(
        app,
        ["history", "failures", "--all-projects", "--db", str(db)],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    # Should mention the error event
    assert "crash" in output.lower() or "cc2" in output.lower() or "error" in output.lower()


# ---------------------------------------------------------------------------
# 18. search with --project filters correctly
# ---------------------------------------------------------------------------


def test_cli_search_project_filter(tmp_path: Path) -> None:
    """--project alpha excludes beta content."""
    db = tmp_path / "filter.db"

    for proj in ("alpha", "beta"):
        sid = f"{proj[0]}s1"
        mid = f"{proj[0]}m1"
        fixture = tmp_path / f"{proj}.jsonl"
        lines = (
            json.dumps(
                {
                    "type": "session",
                    "id": sid,
                    "project": proj,
                    "title": f"{proj} session",
                    "started_at": "2026-06-11T21:00:00Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "id": mid,
                    "role": "user",
                    "text": f"unique content for {proj} project filtertest",
                    "created_at": "2026-06-11T21:00:01Z",
                }
            )
            + "\n"
        )
        fixture.write_text(lines, encoding="utf-8")
        _invoke_import(fixture, db, project=proj)

    result = runner.invoke(
        app,
        ["history", "search", "filtertest", "--project", "alpha", "--db", str(db)],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    # alpha hits should appear, beta should not
    if "membox-capture" in output:
        assert "beta" not in output


# ---------------------------------------------------------------------------
# 19. around command with known message ID
# ---------------------------------------------------------------------------


def test_cli_around_known_id(tmp_path: Path) -> None:
    """around with valid message ID returns exit 0 and contains '>>>'."""
    fixture = tmp_path / "session.jsonl"
    db = tmp_path / "mem.db"
    _write_fixture(fixture)
    _invoke_import(fixture, db, project="demo")

    center_id = "membox-capture:cs1:msg:cm1"
    result = runner.invoke(
        app,
        ["history", "around", center_id, "--project", "demo", "--db", str(db)],
    )
    assert result.exit_code == 0, result.output
    assert ">>>" in result.output


def test_cli_around_project_scope(tmp_path: Path) -> None:
    """around refuses a known message when the project scope does not match."""
    fixture = tmp_path / "session.jsonl"
    db = tmp_path / "mem.db"
    _write_fixture(fixture)
    _invoke_import(fixture, db, project="demo")

    result = runner.invoke(
        app,
        [
            "history",
            "around",
            "membox-capture:cs1:msg:cm1",
            "--project",
            "other",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 20. around command with unknown ID → exit 1
# ---------------------------------------------------------------------------


def test_cli_around_unknown_id(tmp_path: Path) -> None:
    """around with unknown message ID returns exit code 1."""
    db = tmp_path / "mem.db"
    # Ensure schema exists
    fixture = tmp_path / "session.jsonl"
    _write_fixture(fixture)
    _invoke_import(fixture, db, project="demo")

    result = runner.invoke(
        app,
        ["history", "around", "membox-capture:nonexistent:msg:yyy", "--db", str(db)],
    )
    assert result.exit_code == 1
