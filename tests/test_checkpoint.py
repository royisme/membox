"""Tests for the ``membox checkpoint`` one-shot lifecycle wrapper.

Mirrors the house style from :mod:`tests.test_lifecycle_acceptance` —
real fixture files in ``tmp_path``, real ``KnowledgeStore`` on disk,
``CliRunner`` for CLI invocation, and DB-row count assertions for
state. No mocking at internal boundaries; only I/O at the network/time
edges (not exercised here).

Two test styles are mixed because of an asymmetry in the design:

- The ``membox`` adapter (used by the spec's default ``--adapt
  membox-capture``) implements no session discovery, so ``history_pull``
  always returns 0 sessions on its own. Tests that want to exercise the
  full pull→triage→extract chain via the real adapter use the Pi
  adapter with a fixture placed under a discoverable ``session_root``;
  tests that only need to verify the triage+extract span runs against
  pre-existing trace rows pre-import a membox-format fixture (same
  pattern as ``test_lifecycle_acceptance``).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner, Result

from membox.cli import app
from membox.core.history_import import import_history
from membox.core.store import KnowledgeStore

if TYPE_CHECKING:
    import pytest

_PROJECT = "checkpoint-test"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_membox_fixture(
    path: Path,
    *,
    project: str,
    session_id: str,
    messages: list[tuple[str, str]],
) -> Path:
    """Write a membox-format JSONL fixture and return its path."""
    lines: list[str] = [
        json.dumps(
            {
                "type": "session",
                "id": session_id,
                "project": project,
                "title": f"{session_id} fixture",
                "started_at": "2026-06-12T00:00:00Z",
                "source_kind": "membox-capture",
            }
        )
    ]
    for idx, (msg_id, text) in enumerate(messages, start=1):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "id": msg_id,
                    "role": "user",
                    "text": text,
                    "created_at": f"2026-06-12T00:00:0{idx % 10}Z",
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_pi_fixture(
    session_root: Path,
    *,
    cwd: Path,
    session_id: str,
    messages: list[tuple[str, str, str]],
) -> Path:
    """Write a Pi-format JSONL fixture discoverable under *session_root*.

    Pi's ``discover_sessions`` scans ``<session_root>/<project_dir>/`` and
    matches the file's first ``session`` record ``cwd`` against
    ``Path.cwd()``. We create ``<session_root>/<basename(cwd)>/`` and
    match the cwd verbatim.

    Args:
        session_root: Root of the fake Pi session storage tree.
        cwd: Working-directory path the session header advertises.
        session_id: Stable session id used in the Pi header.
        messages: Triples of ``(message_id, role, text)``.

    Returns:
        Path to the written fixture file.
    """
    project_dir = session_root / cwd.name
    project_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = project_dir / f"{session_id}.jsonl"
    lines: list[str] = [
        json.dumps(
            {
                "type": "session",
                "id": session_id,
                "cwd": str(cwd),
                "timestamp": "2026-06-12T00:00:00Z",
            }
        )
    ]
    for idx, (msg_id, role, text) in enumerate(messages, start=1):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "id": msg_id,
                    "message": {
                        "role": role,
                        "content": text,
                        "timestamp": f"2026-06-12T00:00:0{idx % 10}Z",
                    },
                }
            )
        )
    fixture_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fixture_path


def _preimport_fixture(db: str, fixture_path: Path, project: str) -> None:
    """Pre-import a fixture into the store via the lower-level entry point.

    Used by tests that need pre-existing trace rows to assert against,
    since the membox adapter implements no session discovery.
    """
    import_history(KnowledgeStore(db), fixture_path, "membox", project=project)


def _run_checkpoint(db: str, *extra: str) -> Result:
    """Invoke ``membox checkpoint`` with the standard project + db flags."""
    runner = CliRunner()
    return runner.invoke(
        app,
        ["checkpoint", "--db", db, "--project", _PROJECT, "--adapt", "membox", *extra],
    )


# ---------------------------------------------------------------------------
# full chain (pre-imported trace rows; membox adapter has no discovery)
# ---------------------------------------------------------------------------


def test_full_chain_on_fixture_session(tmp_path: Path) -> None:
    """Pre-imported fixture → checkpoint runs triage+extract end-to-end.

    The summary legitimately reads "nothing new to capture" because the
    membox adapter has no discovery and the fixture was pre-populated
    outside the checkpoint run. What matters is that the orchestration
    exercised every step: triage wrote rows, extract created units.
    """
    db = str(tmp_path / "full.db")
    fixture = _write_membox_fixture(
        tmp_path / "full.jsonl",
        project=_PROJECT,
        session_id="full-1",
        messages=[
            (
                "m1",
                "We decided to always verify storage behavior with SQLite tests "
                "before merging any migration.",
            )
        ],
    )
    _preimport_fixture(db, fixture, _PROJECT)

    result = _run_checkpoint(db, "--apply")

    assert result.exit_code == 0, result.output
    store = KnowledgeStore(db)
    triaged = (
        store._conn()
        .execute("SELECT COUNT(*) FROM history_triage WHERE project=?", (_PROJECT,))
        .fetchone()[0]
    )
    units = (
        store._conn()
        .execute("SELECT COUNT(*) FROM memory_units WHERE project=?", (_PROJECT,))
        .fetchone()[0]
    )
    assert triaged > 0, f"expected triaged rows > 0, got {triaged}"
    assert units >= 1, f"expected at least 1 memory unit, got {units}"


def test_idempotent_rerun(tmp_path: Path) -> None:
    """Re-running ``checkpoint --apply`` does not create duplicate units."""
    db = str(tmp_path / "idem.db")
    fixture = _write_membox_fixture(
        tmp_path / "idem.jsonl",
        project=_PROJECT,
        session_id="idem-1",
        messages=[
            (
                "m1",
                "Remember this rule: always run lifecycle fixtures before tuning the gate.",
            )
        ],
    )
    _preimport_fixture(db, fixture, _PROJECT)

    first = _run_checkpoint(db, "--apply")
    assert first.exit_code == 0, first.output
    store = KnowledgeStore(db)
    units_after_first = store._conn().execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
    assert units_after_first >= 1

    second = _run_checkpoint(db, "--apply")
    assert second.exit_code == 0, second.output
    units_after_second = store._conn().execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
    assert units_after_second == units_after_first, (
        f"second run created duplicates: {units_after_first} -> {units_after_second}"
    )


# ---------------------------------------------------------------------------
# real pull path (Pi adapter, session discovery enabled)
# ---------------------------------------------------------------------------


def test_empty_chatter_session_reports_expected_zero_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pulled session of pure chatter triggers the ``0 met the bar`` branch.

    The Pi adapter implements session discovery, so the checkpoint's
    pull step actually finds and imports the fixture — unlike the membox
    adapter. This is what unlocks the empty-cases branch in the summary
    that says "0 met the extraction bar" instead of "nothing new".
    """
    monkeypatch.chdir(tmp_path)
    session_root = tmp_path / "sessions"
    _write_pi_fixture(
        session_root,
        cwd=tmp_path,
        session_id="pi-empty",
        messages=[
            ("p1", "user", "ok thanks"),
            ("p2", "user", "got it, sounds good"),
        ],
    )

    db = str(tmp_path / "empty.db")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "checkpoint",
            "--db",
            db,
            "--project",
            _PROJECT,
            "--adapt",
            "pi",
            "--session-root",
            str(session_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "0 met the extraction bar" in result.output
    store = KnowledgeStore(db)
    units = (
        store._conn()
        .execute("SELECT COUNT(*) FROM memory_units WHERE project=?", (_PROJECT,))
        .fetchone()[0]
    )
    assert units == 0


def test_dry_run_persists_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` against a discoverable fixture writes nothing.

    Uses the Pi adapter so the checkpoint's pull step actually finds the
    fixture. The output uses ``would pull``/``would triage``/``would
    extract`` and the real DB ends up with zero history rows.
    """
    monkeypatch.chdir(tmp_path)
    session_root = tmp_path / "sessions"
    _write_pi_fixture(
        session_root,
        cwd=tmp_path,
        session_id="pi-dry",
        messages=[
            (
                "p1",
                "user",
                "Always run the lifecycle fixtures before tuning the gate.",
            )
        ],
    )

    db = str(tmp_path / "dry.db")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "checkpoint",
            "--db",
            db,
            "--project",
            _PROJECT,
            "--adapt",
            "pi",
            "--session-root",
            str(session_root),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "would pull" in result.output
    assert "would triage" in result.output
    assert "would extract" in result.output

    store = KnowledgeStore(db)
    cur = store._conn()
    assert cur.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM history_triage").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM history_messages").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM history_sessions").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# lease conflict
# ---------------------------------------------------------------------------


def test_lease_conflict_exits_1(tmp_path: Path) -> None:
    """A foreign lease on the project makes ``--apply`` exit 1.

    The lease must come from a foreign pid+hostname so
    ``acquire_lifecycle_lease`` returns False for our process — a
    self-lease would be re-entrant and bypass the conflict check.
    """
    db = str(tmp_path / "lease.db")
    fixture = _write_membox_fixture(
        tmp_path / "lease.jsonl",
        project=_PROJECT,
        session_id="lease-1",
        messages=[
            (
                "m1",
                "Always verify the lifecycle lease spans triage and extract.",
            )
        ],
    )
    _preimport_fixture(db, fixture, _PROJECT)

    # Write a foreign lease directly into the meta table. The shape must
    # satisfy `lease_is_live` (< 30 s old heartbeat) and `lease_is_mine`
    # must return False (different pid/hostname than this test process).
    foreign = json.dumps(
        {
            "pid": 999_999,
            "hostname": "foreign-host",
            "heartbeat": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        }
    )
    store = KnowledgeStore(db)
    store._conn().execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"lifecycle_lease:{_PROJECT}", foreign),
    )

    # Sanity check: from our process the lease must now be unavailable.
    assert store.acquire_lifecycle_lease(_PROJECT) is False

    result = _run_checkpoint(db, "--apply")

    assert result.exit_code == 1, result.output
    assert "another lifecycle operation is in progress" in result.output


# ---------------------------------------------------------------------------
# default adapter / no session_root (defects 1 + 2)
# ---------------------------------------------------------------------------


def test_bare_default_checkpoint_with_no_session_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``membox checkpoint`` with no ``--session-root`` / env exits 0 cleanly.

    The spec's literal default ``--adapt membox-capture`` resolves to the
    membox importer via the ``IMPORTER_FORMATS`` alias, and the absent
    session_root means there is nothing to discover — the result is the
    normal "nothing new to capture" empty-state summary (no ``unknown
    adapter`` error, no traceback).
    """
    monkeypatch.delenv("MEMBOX_SESSION_ROOT", raising=False)
    db = str(tmp_path / "bare.db")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["checkpoint", "--db", db, "--project", _PROJECT, "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "nothing new to capture" in result.output
    assert "unknown adapter" not in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# dry-run verb grammar (defect 3)
# ---------------------------------------------------------------------------


def test_dry_run_verb_grammar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run summary uses base verbs ``pull``/``triage``/``extract`` prefixed with ``would``.

    The earlier wording ``would pulled`` / ``would triaged`` / ``would
    extracted`` was ungrammatical. The spec calls for the same shape as
    apply prefixed with ``would`` — so dry-run reads ``would pull``,
    ``would triage``, ``would extract`` (base form).
    """
    monkeypatch.chdir(tmp_path)
    session_root = tmp_path / "sessions"
    _write_pi_fixture(
        session_root,
        cwd=tmp_path,
        session_id="pi-grammar",
        messages=[
            (
                "g1",
                "user",
                "Always run the lifecycle fixtures before tuning the gate.",
            )
        ],
    )

    db = str(tmp_path / "grammar.db")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "checkpoint",
            "--db",
            db,
            "--project",
            _PROJECT,
            "--adapt",
            "pi",
            "--session-root",
            str(session_root),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "would pull" in result.output
    assert "would triage" in result.output
    assert "would extract" in result.output
    assert "would pulled" not in result.output
    assert "would triaged" not in result.output
    assert "would extracted" not in result.output


def test_apply_verb_grammar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply summary uses past-tense verbs and never the dry-run ``would`` prefix.

    Regression guard: an earlier helper hardcoded ``would`` before the
    triage/extract verbs even on the apply path, so apply read
    ``pulled … → would triaged … → would extracted …``.
    """
    monkeypatch.chdir(tmp_path)
    session_root = tmp_path / "sessions"
    _write_pi_fixture(
        session_root,
        cwd=tmp_path,
        session_id="pi-grammar-apply",
        messages=[
            (
                "g1",
                "user",
                "Always run the lifecycle fixtures before tuning the gate.",
            )
        ],
    )

    db = str(tmp_path / "grammar_apply.db")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "checkpoint",
            "--db",
            db,
            "--project",
            _PROJECT,
            "--adapt",
            "pi",
            "--session-root",
            str(session_root),
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "✓ checkpoint: pulled" in result.output
    assert "→ triaged" in result.output
    assert "→ extracted" in result.output
    assert "would" not in result.output
