"""Tests for the membox CLI and extraction backend selection factory."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from membox.cli import app
from membox.services.embedding import OpenAIEmbedder
from membox.services.extraction import DummyExtractor, OpenAIExtractor, create_default_extractor

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


class _FakeEmbeddingData:
    """Stub for one element of the embeddings response ``data`` list."""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingResponse:
    """Stub for the OpenAI embeddings API response."""

    def __init__(self, embedding: list[float]) -> None:
        self.data = [_FakeEmbeddingData(embedding)]


class _FakeEmbeddingsAPI:
    """Records kwargs passed to ``create`` and returns a stub response."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(kwargs)
        return _FakeEmbeddingResponse([0.1] * self.dim)


class _FakeOpenAIClient:
    """Network-boundary fake for the OpenAI client (embeddings only)."""

    def __init__(self, dim: int) -> None:
        self.embeddings = _FakeEmbeddingsAPI(dim)


class TestCreateDefaultExtractor:
    """Backend selection behavior of create_default_extractor."""

    def test_factory_without_api_key_returns_dummy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        extractor, embedder = create_default_extractor()
        assert isinstance(extractor, DummyExtractor)
        assert embedder is None

    def test_factory_with_use_llm_false_returns_dummy_even_with_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-no-network")
        extractor, embedder = create_default_extractor(use_llm=False)
        assert isinstance(extractor, DummyExtractor)
        assert embedder is None

    def test_factory_with_api_key_returns_openai_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Constructing OpenAIExtractor/OpenAIEmbedder makes no network calls.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-no-network")
        extractor, embedder = create_default_extractor()
        assert isinstance(extractor, OpenAIExtractor)
        assert isinstance(embedder, OpenAIEmbedder)
        # Both share the same underlying client.
        assert extractor.client is embedder.client


class TestOpenAIEmbedderDimensions:
    """OpenAIEmbedder must forward its dim to the embeddings API."""

    def test_embed_passes_dimensions_to_api(self) -> None:
        client = _FakeOpenAIClient(dim=256)
        embedder = OpenAIEmbedder(client=client, dim=256)  # type: ignore[arg-type]
        vector = embedder.embed("hello")
        assert len(vector) == 256
        assert client.embeddings.calls == [
            {"model": "text-embedding-3-small", "input": "hello", "dimensions": 256}
        ]

    def test_embed_default_dim_is_1536(self) -> None:
        client = _FakeOpenAIClient(dim=1536)
        embedder = OpenAIEmbedder(client=client)  # type: ignore[arg-type]
        embedder.embed("hi")
        assert client.embeddings.calls[0]["dimensions"] == 1536


class TestCliIngest:
    """CLI ingest behavior under the Dummy backend."""

    def test_ingest_without_api_key_prints_noop_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello world", "--db", str(db)])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr
        assert "Ingested." in result.stdout

    def test_ingest_no_llm_flag_forces_dummy_with_key_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A fake key is set, but --no-llm must avoid the OpenAI path entirely
        # (the Dummy path makes no network calls).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-no-network")
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello world", "--db", str(db), "--no-llm"])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr

    def test_ingest_writes_document_row_even_with_dummy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(
            app, ["ingest", "some document text", "--db", str(db), "--source", "unit-test"]
        )
        assert result.exit_code == 0
        conn = sqlite3.connect(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_ingest_file_missing_path_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest-file", str(tmp_path / "missing.md"), "--db", str(db)])
        assert result.exit_code == 1
        assert "file not found" in result.stderr

    def test_ingest_file_existing_path_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("membox is a memory layer", encoding="utf-8")
        result = runner.invoke(app, ["ingest-file", str(doc), "--db", str(db)])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr


class TestCliQueryAndListing:
    """CLI query and listing commands."""

    def test_query_without_api_key_prints_noop_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["query", "what is membox?", "--db", str(db)])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr

    def test_list_entities_empty_db_renders_table_header(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["list-entities", "--db", str(db)])
        assert result.exit_code == 0
        assert "Entities" in result.stdout

    def test_list_relations_empty_db_renders_table_header(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["list-relations", "--db", str(db)])
        assert result.exit_code == 0
        assert "Relations" in result.stdout
