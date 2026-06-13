# Tests for the FTS5 fallback seeding path (spec §3.6 hybrid retrieval).
"""FTS fallback: direct chunk search when seed resolution or graph recall is empty."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membox.config import RetrievalConfig
from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.core.store.fts import (
    CJK_TRIGRAM_LIMIT,
    cjk_anchor_terms,
    cjk_trigram_terms,
    fts5_or_query,
)
from membox.core.tokens import est_tokens
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
        assert fts5_or_query("storage backend") == '"storage" OR "backend"'

    def test_strips_fts_specials_and_punctuation(self) -> None:
        assert fts5_or_query('what is "the" backend?') == '"what" OR "is" OR "the" OR "backend"'

    def test_empty_query(self) -> None:
        assert fts5_or_query("   ") == '""'

    def test_cjk_punctuation_stripped(self) -> None:
        assert fts5_or_query("存储后端是什么?") == '"存储后端是什么"'

    def test_cjk_trigram_terms_for_sidecar(self) -> None:
        assert cjk_trigram_terms("苏轼八字") == ["苏轼八", "轼八字"]

    def test_cjk_content_score_counts_maximal_terms_only(self) -> None:
        from membox.core.store.retrieval import _cjk_content_score

        anchors = cjk_anchor_terms("苏轼八字案例")
        # Document covering the full phrase scores its maximal terms only:
        # the 4-gram matches subsume every contained 2/3-gram.
        full = _cjk_content_score("文中提到苏轼八字案例上线。", anchors)
        # Document repeating one short fragment scores just that fragment once.
        frag = _cjk_content_score("案例很多, 案例不少, 还是案例。", anchors)
        assert full > frag
        assert frag == _cjk_content_score("一个案例。", anchors)


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

    def test_results_keep_source_diversity(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        for i in range(6):
            store.insert_document(
                f"project language TypeScript runtime chunk {i}",
                project="membox",
                source_path="membox--HANDOFF.md",
                section=f"Section {i}",
            )
        store.insert_document(
            "project language TypeScript runtime playfun Kishima",
            project="playfun",
            source_path="playfun--HANDOFF.md",
            section="Status",
        )

        chunks = store.fts_fallback_chunks("Which projects use TypeScript runtime?", limit=3)

        source_paths = {chunk[3] for chunk in chunks}
        assert "membox--HANDOFF.md" in source_paths
        assert "playfun--HANDOFF.md" in source_paths

    def test_long_non_cjk_chunks_are_excerpted_around_query_terms(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        filler = "background details unrelated to the answer. " * 120
        store.insert_document(
            f"{filler}\nOpen question: PR #1 review and merge remains pending.\n{filler}",
            project="easymem",
            source_path="easymem--HANDOFF.md",
            section="Open questions",
        )

        chunks = store.fts_fallback_chunks(
            "Which projects have an open question about merging a pull request?",
            limit=1,
        )

        assert len(chunks) == 1
        excerpt = chunks[0][1]
        assert "[excerpt]" in excerpt
        assert "review and merge remains pending" in excerpt
        assert est_tokens(excerpt) < 700

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

    def test_cjk_query_uses_trigram_sidecar(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("苏轼八字案例的命格名是癸水七杀格。")
        query = "苏轼八字案例的命格名是什么?"

        # unicode61 sees the whole Chinese sentence as one token and misses.
        unicode_rows = (
            store._conn()
            .execute(
                "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?;",
                (fts5_or_query(query),),
            )
            .fetchall()
        )
        assert unicode_rows == []

        chunks = store.fts_fallback_chunks(query)
        assert len(chunks) == 1
        assert "癸水七杀格" in chunks[0][1]

    def test_short_cjk_query_uses_like_fallback(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("苏轼案例已经上线。")

        chunks = store.fts_fallback_chunks("苏轼")
        assert len(chunks) == 1
        assert "苏轼" in chunks[0][1]

    def test_cjk_oversized_chunk_is_excerpted(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        filler = "背景资料。" * 1200
        content = f"{filler}\n苏轼八字案例上线,命格名是癸水七杀格,供名人命理案例使用。\n{filler}"
        store.insert_document(content, project="china-zhouyi-app", section="Current state")

        chunks = store.fts_fallback_chunks("苏轼八字案例的命格名是什么?")
        assert len(chunks) == 1
        excerpt = chunks[0][1]
        assert "[excerpt]" in excerpt
        assert "癸水七杀格" in excerpt
        assert est_tokens(excerpt) < 2000

        out = store.fts_fallback_output(chunks, budget=2000)
        assert "癸水七杀格" in out
        assert "1/1 FTS chunks" in out

    def test_cjk_excerpt_drops_weak_redundant_window(self, tmp_path: Path) -> None:
        from membox.core.store.retrieval import _cjk_excerpt, est_tokens

        filler = "背景资料。" * 300
        # All query concepts cluster in one region; a lone weak repeat of one
        # short fragment (命格) sits far away and adds no new anchor terms.
        content = (
            f"{filler}\n苏轼八字案例的命格名是癸水七杀格。\n{filler}\n命格一词再次出现。\n{filler}"
        )
        excerpt = _cjk_excerpt("苏轼八字案例的命格名", content)
        assert "[excerpt]" in excerpt
        assert "癸水七杀格" in excerpt
        # The weak redundant window is dropped, keeping the excerpt compact.
        assert "再次出现" not in excerpt
        assert est_tokens(excerpt) < est_tokens(content)

    def test_cjk_focus_rerank_gets_answer_past_distractors(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document(
            "命格名和学名字段尚未建设, 当前苏轼卡片字段仍是手工撰写。",
            project="china-zhouyi-app",
            section="Open questions",
        )
        store.insert_document(
            "八字到命格名的引擎表计划后续实现, 案例内容先发布。",
            project="china-zhouyi-app",
            section="Decisions",
        )
        filler = "背景资料。" * 900
        store.insert_document(
            f"{filler}\n苏轼八字案例上线, 命格卡片写明丙子辛丑癸亥乙卯, 癸水七杀格。\n{filler}",
            project="china-zhouyi-app",
            section="Current state",
        )

        chunks = store.fts_fallback_chunks("玲珑命理项目中苏轼八字案例的命格名是什么?")
        out = store.fts_fallback_output(chunks, budget=2000)
        assert "苏轼" in out
        assert "癸水" in out
        assert "七杀" in out

    def test_cjk_relation_bm25_uses_trigram_sidecar(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        doc_id = store.insert_document("苏轼八字案例的命格名是癸水七杀格。")
        src = store.create_entity("Su Shi case", "Case", "", None)
        tgt = store.create_entity("Qishage", "Concept", "", None)
        rid = store.upsert_relation(src, tgt, "has_archetype", doc_id)

        scores = store._bm25_scores_for_relations([rid], "苏轼八字案例的命格名是什么?")
        assert rid in scores


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
        # Guards the "fallback" control flow: graph non-empty → no FTS chunks shown.
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        agent.ingest_extracted("Membox uses SQLite.", graph)
        cfg = RetrievalConfig(fusion_mode="fallback")
        out = agent.compact_query("what does Membox use", config=cfg)
        assert "Membox" in out
        assert "FTS chunks" not in out

    def test_empty_db_returns_bare_footer(self, tmp_path: Path) -> None:
        # Guards the "fallback" bare-footer path (no seeds, no FTS matches).
        agent = _make_agent(tmp_path)
        cfg = RetrievalConfig(fusion_mode="fallback")
        out = agent.compact_query("anything", config=cfg)
        assert "(returned 0/0 triples, ~0/0 tokens)" in out

    def test_fallback_disabled_via_config(self, tmp_path: Path) -> None:
        # Guards the "fallback" mode with fts_fallback_k=0 (FTS channel off).
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)
        cfg = RetrievalConfig(fts_fallback_k=0, fusion_mode="fallback")
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


class TestCjkFallbackBehavior:
    """Regression tests for the CJK LIKE/trigram fallback routing rules.

    Covers the three cases mandated by the design doc:

    1. Trigram terms exist but sidecar missing (pre-v5 DB) → fall through to
       unicode61 path, no crash.
    2. Trigram terms exist, sidecar present, MATCH returns 0 rows → return []
       (do NOT degrade to LIKE).
    3. Short CJK query (all runs < 3 chars) → LIKE path used.
    4. project_filter respected on both trigram and short-CJK LIKE paths.
    5. Long CJK query term cap: at most _CJK_TRIGRAM_LIMIT (64) terms emitted.
    """

    def test_trigram_terms_empty_for_short_cjk_runs(self) -> None:
        """_cjk_trigram_terms returns [] when all CJK runs are 1-2 chars long."""
        # "苏轼" is a 2-char run — no 3-char trigram can be formed.
        assert cjk_trigram_terms("苏轼") == []

    def test_cjk_no_sidecar_falls_through_to_unicode61(self, tmp_path: Path) -> None:
        """CJK query with trigram terms on a DB without the sidecar uses unicode61.

        Simulates a pre-v5 database by dropping the sidecar table after the
        store is created.  The result should equal what the unicode61 path
        would return — here the document is ASCII-compatible, so it matches.
        """
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # Insert an ASCII doc that unicode61 can match.
        store.insert_document("membox stores evidence in SQLite databases.")
        # Drop the trigram sidecar to mimic a pre-v5 DB.
        store._conn().execute("DROP TABLE IF EXISTS documents_fts_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_ai_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_ad_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_au_trigram;")

        # A CJK query with trigram terms (>= 3-char run): sidecar absent → []
        # from _cjk_fts_chunks, then fts_fallback_chunks falls through to
        # unicode61.  The doc itself is non-CJK so unicode61 finds it.
        chunks = store.fts_fallback_chunks("苏轼八字案例 databases")
        # unicode61 path should still find "databases" in the doc.
        assert any("SQLite" in c[1] for c in chunks)

    def test_cjk_no_sidecar_pure_cjk_query_no_crash(self, tmp_path: Path) -> None:
        """Pure CJK query on a sidecar-less DB returns [] without crashing.

        With a sidecar-less DB and a pure-CJK query that produces trigram terms,
        _cjk_fts_chunks returns [].  unicode61 also finds nothing because it
        treats the whole run as a single token.  The expected result is [].
        """
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("苏轼八字案例的命格名是癸水七杀格。")
        # Drop the sidecar.
        store._conn().execute("DROP TABLE IF EXISTS documents_fts_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_ai_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_ad_trigram;")
        store._conn().execute("DROP TRIGGER IF EXISTS documents_au_trigram;")

        # Must not crash, and must not return LIKE-based results.
        chunks = store.fts_fallback_chunks("苏轼八字案例的命格名是什么?")
        # The result is empty: sidecar missing → [] from CJK path; unicode61
        # cannot tokenise the run and also returns [].
        assert chunks == []

    def test_cjk_sidecar_present_no_match_returns_empty(self, tmp_path: Path) -> None:
        """CJK MATCH with sidecar present but term not in corpus returns [], not LIKE results.

        Ensures the code does NOT degrade to LIKE when the trigram MATCH finds
        no rows.  A weakly-related doc with overlapping 1-2-char substrings
        must NOT appear in the results.
        """
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # Insert a doc that shares characters with the query but not the trigrams.
        store.insert_document("苏轼是一位诗人。")
        # Query whose 3-char trigrams are absent from the corpus.
        chunks = store.fts_fallback_chunks("黄庭坚写了什么诗歌?")
        # If LIKE were used it might return the doc above (shares "诗" with the
        # query via the anchor "诗歌").  With the fixed code, result must be [].
        assert chunks == []

    def test_cjk_project_filter_trigram_path(self, tmp_path: Path) -> None:
        """project_filter is respected when the trigram sidecar is used."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("苏轼八字案例的命格名是癸水七杀格。", project="china")
        store.insert_document("苏轼八字案例的命格名是癸水七杀格。", project="other")

        chunks = store.fts_fallback_chunks("苏轼八字案例的命格名是什么?", project_filter="china")
        assert len(chunks) == 1
        assert chunks[0][2] == "china"

    def test_cjk_project_filter_like_path(self, tmp_path: Path) -> None:
        """project_filter is respected on the short-CJK LIKE path."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        store.insert_document("苏轼案例已经上线。", project="china")
        store.insert_document("苏轼案例已经上线。", project="other")

        # "苏轼" is a 2-char run → no trigram terms → LIKE path.
        chunks = store.fts_fallback_chunks("苏轼", project_filter="china")
        assert len(chunks) == 1
        assert chunks[0][2] == "china"

    def test_cjk_trigram_term_cap(self) -> None:
        """A very long CJK query emits at most _CJK_TRIGRAM_LIMIT (64) terms."""
        # Build a run of 100 distinct CJK ideographs (U+4E00..U+4E63).
        # A 100-char run yields 98 unique trigrams before dedup, so the cap
        # at 64 is always reached.  Use chr() so the literal stays ASCII.
        long_query = "".join(chr(c) for c in range(0x4E00, 0x4E00 + 100))
        terms = cjk_trigram_terms(long_query)
        assert len(terms) == CJK_TRIGRAM_LIMIT
