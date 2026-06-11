"""Phase 7.5 M2 ingestion-hardening tests.

Covers:
- Migration 0002: fresh-DB upgrade path and v1→v2 upgrade of an existing DB.
- Document metadata columns (project, source_path, section, doc_date, version).
- Idempotent re-ingest: same source_path → new version, old rows retained.
- MemoryAgent.ingest_file: markdown chunking, metadata propagation, non-markdown.
- CLI ingest-file with --project and --doc-date flags.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from membox.cli import app
from membox.core.agent import MemoryAgent, _infer_project
from membox.core.store import KnowledgeStore
from membox.core.store.migrations import (
    MIGRATIONS,
    apply_migrations,
    get_user_version,
    latest_version,
)
from membox.model.schema import IngestMetadata
from membox.services.extraction import DummyExtractor

runner = CliRunner()


# ──────────────────────────────────────────────────────────────────────────────
# 1. Migration tests
# ──────────────────────────────────────────────────────────────────────────────


class TestMigration0002:
    """Migration 0002 adds metadata columns and meta table."""

    def test_latest_version_is_2(self) -> None:
        # Latest is now 4 (M6 adds the ingest_queue table).
        assert latest_version() == 4

    def test_fresh_db_reaches_version_2(self, tmp_path: Path) -> None:
        # Fresh DB is migrated through all versions; current latest is 4.
        store = KnowledgeStore(str(tmp_path / "fresh.db"))
        version = get_user_version(store._conn())
        assert version == 4

    def test_fresh_db_has_all_new_columns(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "fresh.db"))
        cols = {row[1] for row in store._conn().execute("PRAGMA table_info(documents);").fetchall()}
        assert {"project", "source_path", "section", "doc_date", "version"}.issubset(cols)

    def test_fresh_db_has_meta_table(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "fresh.db"))
        tables = {
            row[0]
            for row in store._conn()
            .execute("SELECT name FROM sqlite_master WHERE type='table';")
            .fetchall()
        }
        assert "meta" in tables

    def test_v1_db_upgraded_to_v2(self, tmp_path: Path) -> None:
        """A database that was already at user_version=1 must upgrade correctly."""
        db_path = str(tmp_path / "v1.db")

        # Bootstrap a v1 database manually.
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn, migrations=[(1, MIGRATIONS[0][1])])
        assert get_user_version(conn) == 1
        # Confirm v2 columns are absent at v1.
        cols_v1 = {row[1] for row in conn.execute("PRAGMA table_info(documents);").fetchall()}
        assert "project" not in cols_v1
        conn.close()

        # Open via KnowledgeStore — should trigger v2 + v3 + v4 migrations.
        store = KnowledgeStore(db_path)
        conn2 = store._conn()
        assert get_user_version(conn2) == 4
        cols_v2 = {row[1] for row in conn2.execute("PRAGMA table_info(documents);").fetchall()}
        assert {"project", "source_path", "section", "doc_date", "version"}.issubset(cols_v2)

    def test_v1_db_existing_data_preserved_after_upgrade(self, tmp_path: Path) -> None:
        """Rows written at v1 must survive the ALTER TABLE migration."""
        db_path = str(tmp_path / "v1data.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn, migrations=[(1, MIGRATIONS[0][1])])
        conn.execute("BEGIN;")
        conn.execute("INSERT INTO documents(content, source) VALUES ('old doc', 'old.txt');")
        conn.execute("COMMIT;")
        conn.close()

        store = KnowledgeStore(db_path)
        row = (
            store._conn()
            .execute("SELECT content, source FROM documents WHERE source='old.txt';")
            .fetchone()
        )
        assert row is not None
        assert row[0] == "old doc"

    def test_idx_doc_source_path_created(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "idx.db"))
        indexes = {
            row[1]
            for row in store._conn()
            .execute("SELECT type, name FROM sqlite_master WHERE type='index';")
            .fetchall()
        }
        assert "idx_doc_source_path" in indexes


# ──────────────────────────────────────────────────────────────────────────────
# 2. Document metadata persistence
# ──────────────────────────────────────────────────────────────────────────────


class TestDocumentMetadataPersistence:
    """insert_document stores and retrieves metadata columns correctly."""

    def _make_agent(self, tmp_path: Path) -> MemoryAgent:
        return MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "meta.db"))

    def test_metadata_columns_stored(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        doc_id = agent.store.insert_document(
            "content",
            source="src.md",
            project="myrepo",
            source_path="/abs/path/src.md",
            section="Summary",
            doc_date="2026-06-09",
        )
        row = (
            agent.store._conn()
            .execute(
                "SELECT project, source_path, section, doc_date, version FROM documents WHERE id=?;",
                (doc_id,),
            )
            .fetchone()
        )
        assert row == ("myrepo", "/abs/path/src.md", "Summary", "2026-06-09", 1)

    def test_version_auto_1_on_first_ingest(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        doc_id = agent.store.insert_document(
            "text",
            source_path="/path/to/doc.md",
        )
        row = (
            agent.store._conn()
            .execute("SELECT version FROM documents WHERE id=?;", (doc_id,))
            .fetchone()
        )
        assert row[0] == 1

    def test_metadata_null_when_not_provided(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        doc_id = agent.store.insert_document("text", "legacy.txt")
        row = (
            agent.store._conn()
            .execute(
                "SELECT project, source_path, section, doc_date, version FROM documents WHERE id=?;",
                (doc_id,),
            )
            .fetchone()
        )
        # All new columns should be NULL when not supplied.
        assert all(v is None for v in row)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Idempotent re-ingest versioning
# ──────────────────────────────────────────────────────────────────────────────


class TestIdempotentReIngest:
    """Re-ingesting the same source_path creates new version rows; old rows kept."""

    def test_first_ingest_is_version_1(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "v.db"))
        agent.store.insert_document("v1 content", source_path="/f.md")
        rows = (
            agent.store._conn()
            .execute("SELECT version FROM documents WHERE source_path='/f.md' ORDER BY id;")
            .fetchall()
        )
        assert [r[0] for r in rows] == [1]

    def test_second_ingest_is_version_2(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "v.db"))
        agent.store.insert_document("v1 content", source_path="/f.md")
        agent.store.insert_document("v2 content", source_path="/f.md")
        rows = (
            agent.store._conn()
            .execute("SELECT version FROM documents WHERE source_path='/f.md' ORDER BY id;")
            .fetchall()
        )
        assert [r[0] for r in rows] == [1, 2]

    def test_old_rows_retained_on_reingest(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "v.db"))
        agent.store.insert_document("original", source_path="/f.md")
        agent.store.insert_document("updated", source_path="/f.md")
        rows = (
            agent.store._conn()
            .execute(
                "SELECT content, version FROM documents WHERE source_path='/f.md' ORDER BY id;"
            )
            .fetchall()
        )
        # Both rows must be present; content is unchanged.
        assert len(rows) == 2
        assert rows[0] == ("original", 1)
        assert rows[1] == ("updated", 2)

    def test_multiple_source_paths_independent_versions(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "v.db"))
        agent.store.insert_document("a1", source_path="/a.md")
        agent.store.insert_document("b1", source_path="/b.md")
        agent.store.insert_document("a2", source_path="/a.md")
        rows_a = (
            agent.store._conn()
            .execute("SELECT version FROM documents WHERE source_path='/a.md' ORDER BY id;")
            .fetchall()
        )
        rows_b = (
            agent.store._conn()
            .execute("SELECT version FROM documents WHERE source_path='/b.md' ORDER BY id;")
            .fetchall()
        )
        assert [r[0] for r in rows_a] == [1, 2]
        assert [r[0] for r in rows_b] == [1]

    def test_next_version_for_helper(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "v.db"))
        assert store.next_version_for("/new.md") == 1
        store.insert_document("x", source_path="/new.md")
        assert store.next_version_for("/new.md") == 2


# ──────────────────────────────────────────────────────────────────────────────
# 4. MemoryAgent.ingest_file
# ──────────────────────────────────────────────────────────────────────────────


class TestIngestFile:
    """MemoryAgent.ingest_file chunks markdown and stores metadata."""

    def _agent(self, tmp_path: Path) -> MemoryAgent:
        return MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "agent.db"))

    def test_markdown_splits_into_chunks(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## Alpha\nBody A\n\n## Beta\nBody B\n", encoding="utf-8")
        agent = self._agent(tmp_path)
        results = agent.ingest_file(md)
        assert len(results) == 2

    def test_preamble_plus_sections(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("Preamble.\n\n## Intro\nIntro body.", encoding="utf-8")
        agent = self._agent(tmp_path)
        results = agent.ingest_file(md)
        assert len(results) == 2

    def test_section_metadata_stored(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## Summary\nSome facts.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        row = agent.store._conn().execute("SELECT section, source_path FROM documents;").fetchone()
        assert row[0] == "Summary"
        assert str(md.resolve()) in row[1]

    def test_project_metadata_stored(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md, metadata=IngestMetadata(project="myproject"))
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "myproject"

    def test_project_defaults_to_git_root_name(self, tmp_path: Path) -> None:
        """File inside docs/ subdir → project = git repo root name, not 'docs'."""
        repo = tmp_path / "myrepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        (repo / ".git").mkdir()  # simulate a git repo root
        md = docs / "HANDOFF.md"
        md.write_text("## X\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "myrepo"

    def test_project_defaults_to_parent_dir_when_no_git(self, tmp_path: Path) -> None:
        """No .git anywhere → falls back to immediate parent directory name."""
        sub = tmp_path / "reponame"
        sub.mkdir()
        md = sub / "README.md"
        md.write_text("## X\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "reponame"

    def test_doc_date_stored(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md, metadata=IngestMetadata(doc_date="2026-06-09"))
        row = agent.store._conn().execute("SELECT doc_date FROM documents;").fetchone()
        assert row[0] == "2026-06-09"

    def test_doc_date_defaults_to_file_mtime(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        row = agent.store._conn().execute("SELECT doc_date FROM documents;").fetchone()
        # Should be a valid ISO-8601 date string.
        assert row[0] is not None
        import re

        assert re.match(r"\d{4}-\d{2}-\d{2}", row[0])

    def test_reingest_same_file_creates_new_versions(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## A\nContent.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        agent.ingest_file(md)
        rows = agent.store._conn().execute("SELECT version FROM documents ORDER BY id;").fetchall()
        assert [r[0] for r in rows] == [1, 2]

    def test_reingest_does_not_delete_old_rows(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nContent.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        count_before = agent.store._conn().execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
        agent.ingest_file(md)
        count_after = agent.store._conn().execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
        assert count_after == count_before + 1

    def test_non_markdown_ingested_as_single_chunk(self, tmp_path: Path) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("## Not a heading\nJust text.", encoding="utf-8")
        agent = self._agent(tmp_path)
        results = agent.ingest_file(txt)
        assert len(results) == 1

    def test_empty_sections_skipped(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## A\n\n## B\nBody B.", encoding="utf-8")
        agent = self._agent(tmp_path)
        results = agent.ingest_file(md)
        # Section A has no body — skipped; section B has body.
        assert len(results) == 1

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)
        with pytest.raises(FileNotFoundError):
            agent.ingest_file(tmp_path / "nonexistent.md")

    def test_explicit_project_overrides_git_root(self, tmp_path: Path) -> None:
        """Explicit project metadata always wins over _infer_project."""
        repo = tmp_path / "myrepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        (repo / ".git").mkdir()
        md = docs / "HANDOFF.md"
        md.write_text("## X\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md, metadata=IngestMetadata(project="override"))
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "override"

    def test_ingest_file_git_worktree_dot_git_file(self, tmp_path: Path) -> None:
        """.git as a FILE (worktree) → project = repo root dir name."""
        repo = tmp_path / "worktreerepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        # In a git worktree, .git is a regular file pointing to the main worktree.
        (repo / ".git").write_text("gitdir: /some/other/path/.git/worktrees/wt\n", encoding="utf-8")
        md = docs / "HANDOFF.md"
        md.write_text("## X\nBody.", encoding="utf-8")
        agent = self._agent(tmp_path)
        agent.ingest_file(md)
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "worktreerepo"


# ──────────────────────────────────────────────────────────────────────────────
# 5. CLI ingest-file flags
# ──────────────────────────────────────────────────────────────────────────────


class TestCLIIngestFileFlags:
    """CLI ingest-file --project and --doc-date options propagate to the DB."""

    def test_ingest_file_basic(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## Section\nSome content.", encoding="utf-8")
        db = str(tmp_path / "cli.db")
        result = runner.invoke(
            app,
            ["ingest-file", str(md), "--db", db, "--no-llm", "--sync"],
        )
        assert result.exit_code == 0
        assert "1 chunk" in result.output

    def test_ingest_file_project_flag(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        db = str(tmp_path / "cli.db")
        runner.invoke(
            app,
            ["ingest-file", str(md), "--db", db, "--no-llm", "--sync", "--project", "myrepo"],
        )
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT project FROM documents;").fetchone()
        conn.close()
        assert row[0] == "myrepo"

    def test_ingest_file_doc_date_flag(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        db = str(tmp_path / "cli.db")
        runner.invoke(
            app,
            ["ingest-file", str(md), "--db", db, "--no-llm", "--sync", "--doc-date", "2026-06-09"],
        )
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT doc_date FROM documents;").fetchone()
        conn.close()
        assert row[0] == "2026-06-09"

    def test_ingest_file_missing_file_exits_1(self, tmp_path: Path) -> None:
        db = str(tmp_path / "cli.db")
        result = runner.invoke(
            app,
            ["ingest-file", str(tmp_path / "missing.md"), "--db", db, "--no-llm"],
        )
        assert result.exit_code == 1

    def test_ingest_file_reingest_creates_new_version(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        db = str(tmp_path / "cli.db")
        runner.invoke(app, ["ingest-file", str(md), "--db", db, "--no-llm", "--sync"])
        runner.invoke(app, ["ingest-file", str(md), "--db", db, "--no-llm", "--sync"])
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT version FROM documents ORDER BY id;").fetchall()
        conn.close()
        assert [r[0] for r in rows] == [1, 2]


# ──────────────────────────────────────────────────────────────────────────────
# 6. _infer_project helper unit tests
# ──────────────────────────────────────────────────────────────────────────────


class TestInferProject:
    """Unit tests for the _infer_project() helper in core/agent.py."""

    def test_git_dir_at_repo_root(self, tmp_path: Path) -> None:
        """File inside <repo>/docs/ with a .git directory → project = repo dir name."""
        repo = tmp_path / "myrepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        (repo / ".git").mkdir()
        result = _infer_project((docs / "HANDOFF.md").resolve())
        assert result == "myrepo"

    def test_git_file_worktree(self, tmp_path: Path) -> None:
        """.git as a plain FILE (worktree) is also accepted → project = repo dir name."""
        repo = tmp_path / "worktreerepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        (repo / ".git").write_text("gitdir: /some/other/path/.git/worktrees/wt\n", encoding="utf-8")
        result = _infer_project((docs / "HANDOFF.md").resolve())
        assert result == "worktreerepo"

    def test_no_git_root_falls_back_to_parent_dir(self, tmp_path: Path) -> None:
        """No .git anywhere up to filesystem root → fall back to parent dir name."""
        sub = tmp_path / "scratch"
        sub.mkdir()
        md = sub / "note.txt"
        md.write_text("hello", encoding="utf-8")
        result = _infer_project(md.resolve())
        assert result == "scratch"

    def test_explicit_project_overrides_inferred(self, tmp_path: Path) -> None:
        """Explicit metadata.project always wins; _infer_project is never called."""
        repo = tmp_path / "myrepo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        (repo / ".git").mkdir()
        md = docs / "HANDOFF.md"
        md.write_text("## X\nBody.", encoding="utf-8")
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
        agent.ingest_file(md, metadata=IngestMetadata(project="explicit-override"))
        row = agent.store._conn().execute("SELECT project FROM documents;").fetchone()
        assert row[0] == "explicit-override"
