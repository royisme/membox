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
    MEMORY_UNIT_SYSTEM_PROMPT,
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
        assert "must be 'entities', 'query', or 'units'" in result.stderr

    def test_extract_prompt_for_units_emits_unit_prompt_and_schema(self, tmp_path: Path) -> None:
        """`--for units` emits the MEMORY_UNIT prompt + the ExtractedGraph schema."""
        doc = tmp_path / "doc.md"
        body = "We decided to ship migration 9; the procedure is to call ingest-graph."
        doc.write_text(body, encoding="utf-8")
        result = runner.invoke(app, ["extract-prompt", str(doc), "--for", "units"])
        assert result.exit_code == 0, result.output
        out = result.stdout
        # System prompt is sourced from services/prompts.
        assert MEMORY_UNIT_SYSTEM_PROMPT in out
        # The three rationale fields are spelled out in the final instruction.
        assert "why" in out
        assert "how_to_apply" in out
        assert "next_step" in out
        # Document is wrapped in the prompt.
        assert body in out
        # The ExtractedGraph JSON schema is included so the agent can populate 'units'.
        assert '"units"' in out
        assert '"entities"' in out
        assert '"relations"' in out


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

    def test_ingest_graph_units_stored_matches_fed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the graph carries an ``units`` array, those units are stored too."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("We decided to use migration 9 and added a procedure.", encoding="utf-8")
        payload = json.dumps(
            {
                "entities": [
                    {"name": "Membox", "type": "Project", "description": "Memory layer"},
                ],
                "relations": [
                    {"source": "We", "target": "Membox", "predicate": "decided on"},
                ],
                "units": [
                    {
                        "unit_type": "decision",
                        "title": "Use migration 9 for why/how/next",
                        "content": "We chose to ship rationale columns on memory_units as migration 9.",
                        "context": "M4 Part A2",
                        "why": "so the agent-as-provider path can persist agent rationale",
                        "labels": ["architecture", "storage"],
                    },
                    {
                        "unit_type": "procedure",
                        "title": "Ingest units via ingest-graph",
                        "content": "Caller emits an ExtractedGraph with a units array; membox stores it.",
                        "context": "M4 Part A2",
                        "why": "agents need to be able to write memory units without checkpoint triage",
                        "how_to_apply": "1) run extract-prompt --for units; 2) pipe JSON into ingest-graph --from-json -",
                        "next_step": "extend tests to cover the units-only path",
                        "labels": ["workflow"],
                    },
                ],
            }
        )
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
        assert "stored 1 entities, 1 relations, 2 units (doc_id=" in result.stdout
        assert "fed 1/1/2" in result.stdout

        # Open the DB and verify the two units have why/how/next stored.
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(db))
        rows = (
            store._conn()
            .execute(
                "SELECT title, why, how_to_apply, next_step, status FROM memory_units ORDER BY id"
            )
            .fetchall()
        )
        assert len(rows) == 2
        assert rows[0][0] == "Use migration 9 for why/how/next"
        assert rows[0][1] == "so the agent-as-provider path can persist agent rationale"
        assert rows[0][2] is None
        assert rows[0][3] is None
        assert rows[0][4] == "active_unit"
        assert rows[1][0] == "Ingest units via ingest-graph"
        assert (
            rows[1][1] == "agents need to be able to write memory units without checkpoint triage"
        )
        assert "1) run extract-prompt" in (rows[1][2] or "")
        assert rows[1][3] == "extend tests to cover the units-only path"
        assert rows[1][4] == "active_unit"

    def test_ingest_graph_units_only_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A graph that carries only units (no entities/relations) is valid."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        payload = json.dumps(
            {
                "entities": [],
                "relations": [],
                "units": [
                    {
                        "unit_type": "fact",
                        "title": "SQLite is the storage backend",
                        "content": "Membox stores everything in a local SQLite file.",
                        "labels": ["storage"],
                    },
                ],
            }
        )
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
            input=payload,
        )
        assert result.exit_code == 0, result.output
        assert "stored 0 entities, 0 relations, 1 units (doc_id=" in result.stdout

    def test_ingest_graph_backward_compat_m3_payload_without_units(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """M3 JSON with no 'units' key still validates and ingests unchanged."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("Alice and Bob use Membox.", encoding="utf-8")
        payload = json.dumps(
            {
                "entities": [
                    {"name": "Alice", "type": "Person", "description": ""},
                    {"name": "Bob", "type": "Person", "description": ""},
                    {"name": "Membox", "type": "Project", "description": "Memory layer"},
                ],
                "relations": [
                    {"source": "Alice", "target": "Membox", "predicate": "uses"},
                    {"source": "Bob", "target": "Membox", "predicate": "uses"},
                ],
            }
        )
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
        assert "stored 3 entities, 2 relations, 0 units (doc_id=" in result.stdout
        assert "fed 3/2/0" in result.stdout

    def test_ingest_graph_unknown_label_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Agent-emitted units with unknown labels exit 1 with a clear message (no traceback)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        bad = {
            "entities": [],
            "relations": [],
            "units": [
                {
                    "unit_type": "fact",
                    "title": "Mislabeled claim",
                    "content": "This unit uses a label outside the closed set.",
                    "labels": ["nonsense-label"],
                }
            ],
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
        assert "invalid ExtractedGraph JSON" in result.stderr
        assert "Traceback" not in result.stderr


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
