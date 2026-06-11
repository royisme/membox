# Tests for the FTS5 fallback seeding path (spec §3.6 hybrid retrieval).
"""FTS fallback: direct chunk search when seed resolution or graph recall is empty."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membox.config import RetrievalConfig
from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.core.store.retrieval import _fts5_or_query
from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
from membox.services.extraction import DummyExtractor

if TYPE_CHECKING:
    from pathlib import Path


class _SeedExtractor(DummyExtractor):
    """Extractor stub that returns fixed query seeds (which may not resolve)."""

    def __init__(self, seeds: list[str]) -> None:
        self._seeds = seeds

    def extract_query_entities(self, query: str) -> list[str]:
        return list(self._seeds)


def _make_agent(tmp_path: Path, extractor: DummyExtractor | None = None) -> MemoryAgent:
    return MemoryAgent(
        extractor=extractor or DummyExtractor(),
        db_path=str(tmp_path / "fts.db"),
    )


_EMPTY_GRAPH = ExtractedGraph(entities=[], relations=[])


class TestFts5OrQuery:
    """Tokenisation of natural-language questions into OR-of-tokens MATCH."""

    def test_multi_token(self) -> None:
        assert _fts5_or_query("storage backend") == '"storage" OR "backend"'

    def test_strips_fts_specials_and_punctuation(self) -> None:
        assert _fts5_or_query('what is "the" backend?') == '"what" OR "is" OR "the" OR "backend"'

    def test_empty_query(self) -> None:
        assert _fts5_or_query("   ") == '""'

    def test_cjk_punctuation_stripped(self) -> None:
        assert _fts5_or_query("存储后端是什么?") == '"存储后端是什么"'


class TestFallbackChunks:
    """KnowledgeStore.fts_fallback_chunks behaviour."""

    def test_returns_matching_chunk(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("SQLite is the storage backend for membox.")
        chunks = store.fts_fallback_chunks("what storage backend is used")
        assert len(chunks) == 1
        assert "SQLite" in chunks[0][1]

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("SQLite is the storage backend.")
        assert store.fts_fallback_chunks("zzzunmatchable") == []

    def test_project_filter(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("alpha uses Redis caching.", project="alpha")
        store.insert_document("beta uses Redis caching.", project="beta")
        chunks = store.fts_fallback_chunks("Redis caching", project_filter="alpha")
        assert len(chunks) == 1
        assert chunks[0][2] == "alpha"

    def test_limit_respected(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        for i in range(8):
            store.insert_document(f"membox chunk number {i} about retrieval.")
        chunks = store.fts_fallback_chunks("membox retrieval", limit=3)
        assert len(chunks) == 3

    def test_version_dedup_keeps_latest(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document(
            "membox retrieval status: baseline.",
            source_path="docs/HANDOFF.md",
            section="Status",
        )
        store.insert_document(
            "membox retrieval status: fallback implemented.",
            source_path="docs/HANDOFF.md",
            section="Status",
        )
        chunks = store.fts_fallback_chunks("membox retrieval status")
        assert len(chunks) == 1
        assert "fallback implemented" in chunks[0][1]

    def test_zero_limit_returns_empty(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("anything at all")
        assert store.fts_fallback_chunks("anything", limit=0) == []


class TestFallbackOutput:
    """KnowledgeStore.fts_fallback_output budgeting and footer."""

    def test_renders_chunk_with_provenance_and_footer(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        chunks: list[tuple[int, str, str | None, str | None, str | None, str | None]] = [
            (1, "SQLite is the backend.", "membox", "docs/HANDOFF.md", "Status", "2026-06-09")
        ]
        out = store.fts_fallback_output(chunks, budget=2000)
        assert "SQLite is the backend." in out
        assert "[membox docs/HANDOFF.md ## Status 2026-06-09]" in out
        assert "1/1 FTS chunks" in out
        assert "returned 0/0 triples" in out

    def test_budget_truncation(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        chunks: list[tuple[int, str, str | None, str | None, str | None, str | None]] = [
            (1, "short", None, None, None, None),
            (2, "x" * 4000, None, None, None, None),
        ]
        out = store.fts_fallback_output(chunks, budget=50)
        assert "short" in out
        assert "x" * 4000 not in out
        assert "1/2 FTS chunks" in out
        assert "raise --budget for more" in out

    def test_empty_chunks_yields_bare_footer(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        out = store.fts_fallback_output([], budget=2000)
        assert out == "(returned 0/0 triples, 0/0 FTS chunks, ~0/2,000 tokens)"


class TestAgentFallbackIntegration:
    """compact_query falls back to FTS when graph retrieval is empty."""

    def test_no_seeds_extracted_falls_back(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)
        out = agent.compact_query("what storage backend is used")
        assert "SQLite" in out
        assert "FTS chunks" in out

    def test_seeds_unresolved_falls_back(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path, _SeedExtractor(["NoSuchEntity"]))
        agent.ingest_extracted("Membox stores evidence chunks in SQLite.", _EMPTY_GRAPH)
        out = agent.compact_query("evidence chunks SQLite")
        assert "evidence chunks" in out
        assert "FTS chunks" in out

    def test_seeds_resolved_but_no_relations_falls_back(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[ExtractedEntity(name="Membox", type="Project")],
            relations=[],
        )
        agent.ingest_extracted("Membox is a local memory layer.", graph)
        out = agent.compact_query("Membox memory layer")
        assert "local memory layer" in out
        assert "FTS chunks" in out

    def test_graph_hit_does_not_fall_back(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        agent.ingest_extracted("Membox uses SQLite.", graph)
        out = agent.compact_query("what does Membox use")
        assert "Membox" in out
        assert "FTS chunks" not in out

    def test_empty_db_returns_bare_footer(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        out = agent.compact_query("anything")
        assert "(returned 0/0 triples, ~0/0 tokens)" in out

    def test_fallback_disabled_via_config(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)
        cfg = RetrievalConfig(fts_fallback_k=0)
        out = agent.compact_query("what storage backend is used", config=cfg)
        assert "SQLite" not in out
        assert "(returned 0/0 triples, ~0/0 tokens)" in out

    def test_project_filter_scopes_fallback(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("alpha uses Redis.", _EMPTY_GRAPH, project="alpha")
        agent.ingest_extracted("beta uses Redis.", _EMPTY_GRAPH, project="beta")
        out = agent.compact_query("who uses Redis", project_filter="alpha")
        assert "alpha uses Redis." in out
        assert "beta uses Redis." not in out

    def test_pending_note_appended_after_fallback(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)
        agent.store.enqueue_ingest("queued content", project=None, source_path=None)
        out = agent.compact_query("what storage backend is used")
        assert "FTS chunks" in out
        assert "pending" in out
