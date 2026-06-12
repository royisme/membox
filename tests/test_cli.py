"""Tests for the membox CLI and extraction backend selection factory."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from membox.cli import app
from membox.core.store import KnowledgeStore
from membox.model.schema import (
    MemorySourceKind,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    MemoryUnitType,
)
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

    def __init__(self, embeddings: list[list[float]]) -> None:
        self.data = [_FakeEmbeddingData(embedding) for embedding in embeddings]


class _FakeEmbeddingsAPI:
    """Records kwargs passed to ``create`` and returns a stub response."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(kwargs)
        payload = kwargs["input"]
        texts = payload if isinstance(payload, list) else [payload]
        embeddings = [[float(len(str(text)))] * self.dim for text in texts]
        return _FakeEmbeddingResponse(embeddings)


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
    """OpenAIEmbedder dimensions, cache, and batching behavior."""

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

    def test_embed_cache_avoids_duplicate_api_calls(self) -> None:
        client = _FakeOpenAIClient(dim=4)
        embedder = OpenAIEmbedder(client=client, dim=4, cache_size=10)  # type: ignore[arg-type]

        assert embedder.embed("repeat") == [6.0] * 4
        assert embedder.embed("repeat") == [6.0] * 4

        assert len(client.embeddings.calls) == 1

    def test_embed_cache_evicts_oldest_entry_at_cap(self) -> None:
        client = _FakeOpenAIClient(dim=4)
        embedder = OpenAIEmbedder(client=client, dim=4, cache_size=1)  # type: ignore[arg-type]

        embedder.embed("a")
        embedder.embed("b")
        embedder.embed("a")

        assert [call["input"] for call in client.embeddings.calls] == ["a", "b", "a"]

    def test_embed_many_batches_unique_cache_misses(self) -> None:
        client = _FakeOpenAIClient(dim=4)
        embedder = OpenAIEmbedder(
            client=client,  # type: ignore[arg-type]
            dim=4,
            cache_size=10,
            batch_size=2,
        )

        vectors = embedder.embed_many(["alpha", "beta", "alpha", "gamma"])

        assert vectors == [[5.0] * 4, [4.0] * 4, [5.0] * 4, [5.0] * 4]
        assert [call["input"] for call in client.embeddings.calls] == [
            ["alpha", "beta"],
            "gamma",
        ]


class TestCliIngest:
    """CLI ingest behavior under the Dummy backend."""

    def test_ingest_sync_without_api_key_prints_noop_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello world", "--db", str(db), "--sync"])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr
        assert "Ingested." in result.stdout

    def test_ingest_default_is_async_enqueue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # M6: the default path enqueues without LLM calls or chunking; the
        # no-op notice belongs to the materialization path, not the enqueue.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "hello world", "--db", str(db), "--no-spawn"])
        assert result.exit_code == 0
        assert "Enqueued" in result.stdout
        assert "no-op extractor" not in result.stderr

    def test_ingest_no_llm_flag_forces_dummy_with_key_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A fake key is set, but --no-llm must avoid the OpenAI path entirely
        # (the Dummy path makes no network calls).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-no-network")
        db = tmp_path / "memory.db"
        result = runner.invoke(
            app, ["ingest", "hello world", "--db", str(db), "--no-llm", "--sync"]
        )
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr

    def test_ingest_sync_writes_document_row_even_with_dummy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(
            app,
            ["ingest", "some document text", "--db", str(db), "--source", "unit-test", "--sync"],
        )
        assert result.exit_code == 0
        conn = sqlite3.connect(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_ingest_async_writes_queue_row_not_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        result = runner.invoke(app, ["ingest", "some document text", "--db", str(db), "--no-spawn"])
        assert result.exit_code == 0
        conn = sqlite3.connect(db)
        try:
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            queued = conn.execute("SELECT COUNT(*) FROM ingest_queue").fetchone()[0]
        finally:
            conn.close()
        assert docs == 0
        assert queued == 1

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
        result = runner.invoke(app, ["ingest-file", str(doc), "--db", str(db), "--no-spawn"])
        assert result.exit_code == 0
        assert "Enqueued" in result.stdout

    def test_ingest_file_sync_prints_noop_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("membox is a memory layer", encoding="utf-8")
        result = runner.invoke(app, ["ingest-file", str(doc), "--db", str(db), "--sync"])
        assert result.exit_code == 0
        assert "no-op extractor" in result.stderr

    def test_ingest_concurrency_flag_is_accepted_and_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--concurrency 2 is accepted and the agent is created with that value."""
        import membox.cli.commands.ingest as _ingest_mod

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        captured: list[int | None] = []

        from membox.cli._common import make_agent as orig_make_agent

        def _spy_make_agent(
            db_path: str,
            no_llm: bool = False,
            warn: bool = False,
            concurrency: int | None = None,
        ) -> object:
            captured.append(concurrency)
            return orig_make_agent(db_path, no_llm=no_llm, warn=warn, concurrency=concurrency)

        monkeypatch.setattr(_ingest_mod, "make_agent", _spy_make_agent)
        result = runner.invoke(
            app, ["ingest", "hello world", "--db", str(db), "--concurrency", "2", "--sync"]
        )
        assert result.exit_code == 0
        assert captured == [2], f"expected concurrency=2, got {captured}"

    def test_ingest_omitting_concurrency_flag_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Omitting --concurrency passes None to make_agent; env var is honoured."""
        import membox.cli.commands.ingest as _ingest_mod

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("MEMBOX_INGEST_CONCURRENCY", "3")
        db = tmp_path / "memory.db"

        from membox.cli._common import make_agent as orig_make_agent

        captured: list[int | None] = []

        def _spy_make_agent(
            db_path: str,
            no_llm: bool = False,
            warn: bool = False,
            concurrency: int | None = None,
        ) -> object:
            captured.append(concurrency)
            return orig_make_agent(db_path, no_llm=no_llm, warn=warn, concurrency=concurrency)

        monkeypatch.setattr(_ingest_mod, "make_agent", _spy_make_agent)
        result = runner.invoke(app, ["ingest", "hello world", "--db", str(db), "--sync"])
        assert result.exit_code == 0
        # The CLI does not set the flag, so make_agent receives concurrency=None;
        # make_agent internally reads the env var via MemboxConfig.
        assert captured == [None]
        # Verify the sync path completed successfully (document row written).
        conn = sqlite3.connect(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_ingest_file_concurrency_flag_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--concurrency is accepted on ingest-file and forwarded to make_agent."""
        import membox.cli.commands.ingest as _ingest_mod

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        doc = tmp_path / "doc.md"
        doc.write_text("membox is a memory layer", encoding="utf-8")
        captured: list[int | None] = []

        from membox.cli._common import make_agent as orig_make_agent

        def _spy_make_agent(
            db_path: str,
            no_llm: bool = False,
            warn: bool = False,
            concurrency: int | None = None,
        ) -> object:
            captured.append(concurrency)
            return orig_make_agent(db_path, no_llm=no_llm, warn=warn, concurrency=concurrency)

        monkeypatch.setattr(_ingest_mod, "make_agent", _spy_make_agent)
        result = runner.invoke(
            app,
            ["ingest-file", str(doc), "--db", str(db), "--concurrency", "4", "--sync"],
        )
        assert result.exit_code == 0
        assert captured == [4], f"expected concurrency=4, got {captured}"


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

    def test_query_include_memory_uses_project_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`membox query --include-memory` prints scoped memory rows."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        db = tmp_path / "memory.db"
        store = KnowledgeStore(str(db))
        store.create_memory_unit(
            MemoryUnitRecord(
                project="membox",
                unit_type=MemoryUnitType.DECISION,
                status=MemoryUnitStatus.ACTIVE_UNIT,
                title="CLI memory fusion",
                content="Query include-memory prints scoped memory rows.",
                importance_score=0.8,
                confidence_score=0.8,
                labels=["retrieval"],
                sources=[
                    MemoryUnitSource(
                        source_kind=MemorySourceKind.MANUAL,
                        source_ref="manual:cli-memory",
                        quote="CLI memory fusion",
                    )
                ],
            )
        )

        result = runner.invoke(
            app,
            [
                "query",
                "CLI memory fusion",
                "--db",
                str(db),
                "--project",
                "membox",
                "--include-memory",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Relevant memory" in result.output
        assert "CLI memory fusion" in result.output

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
