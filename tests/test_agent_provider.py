"""Tests for the agent-as-LLM-provider CLI protocol and the no-extractor footgun fix.

Covers:
- ``membox extract-prompt <file|->`` output shape for both ``--for entities``
  (default) and ``--for query`` — the prompt + schema + wrapped document, plus
  stdin input via ``CliRunner.invoke(input=...)``.
- ``membox ingest-graph --from-json -`` happy path: stored entity/relation
  counts equal fed counts; rows are actually present in the SQLite store.
- Invalid JSON / schema-violating JSON: exit code 1 with a retriable message
  (no traceback).
- No-extractor ``ingest`` warning fires in BOTH the sync and the default async
  (enqueue) paths, on ``ingest`` and ``ingest-file``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from membox.cli import app
from membox.services.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    QUERY_KEYWORDS_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


runner = CliRunner()


class TestExtractPrompt:
    """`membox extract-prompt` output shape and stdin/file handling."""

    def test_extract_prompt_from_file_emits_prompt_and_schema(self, tmp_path: Path) -> None:
        doc = tmp_path / "doc.md"
        body = "Membox is a local knowledge graph + RAG memory layer."
        doc.write_text(body, encoding="utf-8")
        result = runner.invoke(app, ["extract-prompt", str(doc)])
        assert result.exit_code == 0, result.output
        out = result.stdout
        # System prompt sourced from services/prompts/.
        assert EXTRACTION_SYSTEM_PROMPT in out
        # Document content wrapped under a clear section.
        assert body in out
        # JSON schema for ExtractedGraph (entities + relations + predicate field).
        assert '"entities"' in out
        assert '"relations"' in out
        assert '"predicate"' in out
        # The model name ExtractedGraph should also appear in the schema dump.
        assert "ExtractedGraph" in out
        # Section headers are stable.
        assert "## Instructions" in out
        assert "## Document" in out
        assert "## Output JSON schema" in out

    def test_extract_prompt_for_query_uses_query_prompt_and_array_schema(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "q.txt"
        doc.write_text("Which project uses SQLite?", encoding="utf-8")
        result = runner.invoke(app, ["extract-prompt", str(doc), "--for", "query"])
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert QUERY_KEYWORDS_SYSTEM_PROMPT in out
        assert "Which project uses SQLite?" in out
        # Array-of-strings schema marker.
        assert '"array"' in out
        # Should NOT include the ExtractedGraph entity/relation schema keys.
        assert '"predicate"' not in out

    def test_extract_prompt_stdin_uses_dash(self) -> None:
        body = "stdin-fed document text"
        result = runner.invoke(app, ["extract-prompt", "-"], input=body)
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert EXTRACTION_SYSTEM_PROMPT in out
        assert body in out

    def test_extract_prompt_missing_file_exits_1(self) -> None:
        result = runner.invoke(app, ["extract-prompt", "/nonexistent/path.md"])
        assert result.exit_code == 1
        assert "file not found" in result.stderr

    def test_extract_prompt_invalid_for_value_exits_1(self, tmp_path: Path) -> None:
        doc = tmp_path / "doc.txt"
        doc.write_text("hi", encoding="utf-8")
        result = runner.invoke(app, ["extract-prompt", str(doc), "--for", "bogus"])
        assert result.exit_code == 1
        assert "must be 'entities' or 'query'" in result.stderr


class TestIngestGraph:
    """`membox ingest-graph` validation and storage behavior."""

    @staticmethod
    def _valid_graph() -> dict[str, object]:
        return {
            "entities": [
                {"name": "Alice", "type": "Person", "description": "Engineer"},
                {"name": "Bob", "type": "Person", "description": "Researcher"},
                {"name": "Membox", "type": "Project", "description": "Memory layer"},
            ],
            "relations": [
                {"source": "Alice", "target": "Membox", "predicate": "uses"},
                {"source": "Bob", "target": "Membox", "predicate": "uses"},
            ],
        }

    def test_ingest_graph_happy_path_stored_matches_fed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("Alice and Bob both use Membox.", encoding="utf-8")

        payload = json.dumps(self._valid_graph())
        result = runner.invoke(
            app,
            [
                "ingest-graph",
                "--from-json",
                "-",
                "--source",
                str(doc),
                "--db",
                str(db),
            ],
            input=payload,
        )
        assert result.exit_code == 0, result.output
        assert "stored 3 entities" in result.stdout
        assert "stored 3 entities, 2 relations" in result.stdout
        assert "doc_id=" in result.stdout
        # ingest-graph IS the agent-as-provider flow — it must not emit the
        # no-extractor footgun pointer (that would be a confusing self-reference).
        assert "no extractor configured" not in result.stderr
        assert "extract-prompt" not in result.stderr

        # Rows actually present in SQLite.
        conn = sqlite3.connect(db)
        try:
            entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        assert entity_count == 3
        assert relation_count == 2
        assert doc_count == 1

    def test_ingest_graph_via_file_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        payload_file = tmp_path / "graph.json"
        payload_file.write_text(json.dumps(self._valid_graph()), encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "ingest-graph",
                "--from-json",
                str(payload_file),
                "--source",
                "test-provenance",
                "--db",
                str(db),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "stored 3 entities, 2 relations" in result.stdout

    def test_ingest_graph_malformed_json_exits_1_with_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(
            app,
            [
                "ingest-graph",
                "--from-json",
                "-",
                "--source",
                "label-only",
                "--db",
                str(db),
            ],
            input="{not valid json",
        )
        assert result.exit_code == 1
        err = result.stderr
        assert "Error: invalid JSON" in err
        assert "extract-prompt" in err
        assert "Traceback" not in err

    def test_ingest_graph_schema_violation_exits_1_with_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        # Missing required 'relations' key, and a relation missing 'predicate'.
        bad = {
            "entities": [{"name": "X", "type": "T", "description": ""}],
            "relations": [{"source": "X", "target": "Y"}],
        }
        result = runner.invoke(
            app,
            [
                "ingest-graph",
                "--from-json",
                "-",
                "--source",
                "label-only",
                "--db",
                str(db),
            ],
            input=json.dumps(bad),
        )
        assert result.exit_code == 1
        err = result.stderr
        assert "Error: invalid ExtractedGraph JSON" in err
        assert "extract-prompt" in err
        assert "Traceback" not in err

    def test_ingest_graph_missing_file_exits_1(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest-graph",
                "--from-json",
                str(tmp_path / "missing.json"),
                "--source",
                "label-only",
            ],
        )
        assert result.exit_code == 1
        assert "file not found" in result.stderr


class TestIngestFootgunFix:
    """The agent-as-provider footgun fix: no-extractor ingest must warn in
    BOTH sync and async (default enqueue) paths."""

    def test_ingest_sync_warns_with_agent_as_provider_pointer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello", "--db", str(db), "--sync"])
        assert result.exit_code == 0
        assert "no extractor configured" in result.stderr
        assert "extract-prompt" in result.stderr
        assert "ingest-graph" in result.stderr

    def test_ingest_async_default_warns_with_agent_as_provider_pointer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello", "--db", str(db), "--no-spawn"])
        assert result.exit_code == 0
        assert "Enqueued" in result.stdout
        assert "no extractor configured" in result.stderr
        assert "extract-prompt" in result.stderr
        assert "ingest-graph" in result.stderr

    def test_ingest_file_async_default_warns_with_agent_as_provider_pointer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("some text", encoding="utf-8")
        result = runner.invoke(app, ["ingest-file", str(doc), "--db", str(db), "--no-spawn"])
        assert result.exit_code == 0
        assert "Enqueued" in result.stdout
        assert "no extractor configured" in result.stderr
        assert "extract-prompt" in result.stderr
