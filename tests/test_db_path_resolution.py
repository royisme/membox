"""Tests for DB path resolution: default, MEMBOX_DB env var, and --db flag override."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from membox.cli import app
from membox.core.store import KnowledgeStore

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


def test_default_db_path_ends_with_expected_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``default_db_path()`` returns a path under home ending in ``.membox/membox.db``."""
    # Patch Path.home() directly: Windows prefers USERPROFILE over HOME, so
    # env-only monkeypatching does not reliably control this seam in CI.
    from membox.config import default_db_path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = default_db_path()
    assert result == str(tmp_path / ".membox" / "membox.db"), f"unexpected: {result}"


def test_cli_honors_membox_db_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI writes to MEMBOX_DB path when no ``--db`` flag is given."""
    env_db = tmp_path / "env.db"
    monkeypatch.setenv("MEMBOX_DB", str(env_db))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(app, ["list-entities"])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert env_db.exists(), f"expected DB at {env_db} but it was not created"


def test_explicit_db_flag_overrides_membox_db_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit ``--db`` takes precedence over ``MEMBOX_DB`` env var."""
    env_db = tmp_path / "env.db"
    flag_db = tmp_path / "flag.db"
    monkeypatch.setenv("MEMBOX_DB", str(env_db))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(app, ["list-entities", "--db", str(flag_db)])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert flag_db.exists(), f"expected DB at {flag_db} but it was not created"
    assert not env_db.exists(), f"env DB {env_db} should NOT have been created"


def test_connection_manager_creates_parent_dirs(tmp_path: Path) -> None:
    """``KnowledgeStore`` auto-creates nested parent directories on first use."""
    deep_db = tmp_path / "nested" / "deep" / "x.db"
    assert not deep_db.parent.exists(), "precondition: parent dir must not exist yet"
    store = KnowledgeStore(str(deep_db))
    # A trivial read op forces the connection (and migration) to run.
    entities = store.list_entities()
    assert isinstance(entities, list)
    assert deep_db.exists(), f"expected DB file at {deep_db}"
