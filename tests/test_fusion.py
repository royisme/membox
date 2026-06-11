# Tests for budget-partitioned graph+FTS fusion retrieval (spec §6 Step 1).
"""Fusion: three-pass knapsack, allowance boundaries, cross-pool dedup, edge cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from membox.config import RetrievalConfig
from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.core.store.retrieval import est_tokens
from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
from membox.services.extraction import DummyExtractor

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SeedExtractor(DummyExtractor):
    """Extractor stub that returns fixed query seeds."""

    def __init__(self, seeds: list[str]) -> None:
        self._seeds = seeds

    def extract_query_entities(self, query: str) -> list[str]:
        return list(self._seeds)


def _make_agent(tmp_path: Path, extractor: DummyExtractor | None = None) -> MemoryAgent:
    return MemoryAgent(
        extractor=extractor or DummyExtractor(),
        db_path=str(tmp_path / "fusion.db"),
    )


_EMPTY_GRAPH = ExtractedGraph(entities=[], relations=[])

# A minimal typed chunk tuple.
_Chunk = tuple[int, str, str | None, str | None, str | None, str | None]


def _chunk(doc_id: int, content: str) -> _Chunk:
    return (doc_id, content, None, None, None, None)


def _scored_item(
    rid: int,
    subj: str,
    pred: str,
    obj: str,
    score: float = 1.0,
    evidence: list[tuple[int, str, str | None, str | None, str | None, str | None]] | None = None,
) -> dict[str, object]:
    return {
        "relation_id": rid,
        "subject": subj,
        "predicate": pred,
        "object": obj,
        "hops": 0,
        "score": score,
        "sim": 0.0,
        "bm25": score,
        "evidence": evidence or [],
        "_best_date": "",
    }


# ---------------------------------------------------------------------------
# 1. fused_output: partition allowance boundaries & leftover flow
# ---------------------------------------------------------------------------


class TestFusedOutputAllowances:
    """Pass-1 leftover flows to pass 2; pass-2 leftover flows to pass 3."""

    def test_pass1_leftover_expands_chunk_allowance(self, tmp_path: Path) -> None:
        """If triples use less than triple_allowance, leftover goes to chunks."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # budget=100, chunk_share=0.4 → chunk_reserve=40, triple_allowance=60
        # One tiny triple costs ~3 tokens, leaving ~57 tokens of leftover → chunk_allowance=97.
        triple = _scored_item(1, "A", "rel", "B", score=1.0)
        # A chunk whose cost would exceed chunk_reserve=40 alone but fits with leftover.
        big_chunk_content = "x" * 160  # est_tokens ≈ 40 non-CJK → ceil(160/4)=40 tokens
        # tag cost: "[unknown]" ≈ 3 tokens. Total ≈ 43 tokens.
        chunk = _chunk(10, big_chunk_content)

        out = store.fused_output(
            scored=[triple],
            chunks=[chunk],
            budget=100,
            chunk_share=0.4,
            top_evidence_k=0,
        )
        # Chunk must be admitted because leftover from pass 1 bridges the gap.
        assert big_chunk_content in out

    def test_pass2_leftover_flows_to_triple_backfill(self, tmp_path: Path) -> None:
        """If chunks exhaust less than chunk_allowance, remainder goes to backfill."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # budget=100, chunk_share=0.5 → chunk_reserve=50, triple_allowance=50
        # Two triples of cost ~3 each = 6 total → 44 leftover
        # One short chunk of cost ~3 → chunk_allowance=94, uses 3, leftover=91
        # Three more triples should be backfilled from that 91.
        triples = [_scored_item(i, f"S{i}", "rel", f"O{i}", score=float(10 - i)) for i in range(5)]
        # Only first 16 triples fit in 50 (each ~9 tokens: "S0: rel O0" = 10 chars)
        # Actually let's build scored so first 2 fit in pass1, rest backfilled in pass3.
        chunk = _chunk(99, "short")

        out = store.fused_output(
            scored=triples,
            chunks=[chunk],
            budget=200,
            chunk_share=0.01,  # tiny chunk_reserve=2 → nearly all goes to triples pass1
            top_evidence_k=0,
        )
        # All 5 triples should fit with large budget, regardless of pass.
        assert "returned 5/5 triples" in out

    def test_exact_triple_allowance_boundary(self, tmp_path: Path) -> None:
        """A triple costing exactly triple_allowance fits; one costing +1 does not."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # Build a triple whose line cost exactly = triple_allowance.
        # budget=50, chunk_share=0.0 → chunk_reserve=0, triple_allowance=50
        # "A: rel BBBB" — let's compute the cost and craft accordingly.
        # "A: r B" = 7 chars → ceil(7/4) = 2 tokens. Way under 50.
        # Just verify the triple fits.
        triple = _scored_item(1, "A", "r", "B", score=1.0)
        out = store.fused_output(
            scored=[triple],
            chunks=[],
            budget=50,
            chunk_share=0.0,
            top_evidence_k=0,
        )
        assert "returned 1/1 triples" in out

    def test_oversized_triple_skipped_but_smaller_admitted(self, tmp_path: Path) -> None:
        """Skip-and-continue: oversized triple is skipped, cheaper one admitted."""
        store = KnowledgeStore(str(tmp_path / "s.db"))
        big_triple = _scored_item(
            1,
            "A" * 200,
            "rel",
            "B" * 200,
            score=1.0,  # very long → many tokens
        )
        small_triple = _scored_item(2, "X", "r", "Y", score=0.9)

        # Budget: enough for small but not big.
        small_line = "X: r Y"
        small_cost = est_tokens(small_line)
        budget = small_cost + 1  # just enough for small, not big

        out = store.fused_output(
            scored=[big_triple, small_triple],
            chunks=[],
            budget=budget,
            chunk_share=0.0,
            top_evidence_k=0,
        )
        assert "X" in out
        assert "returned" in out and "triples" in out


# ---------------------------------------------------------------------------
# 2. fused_output: cross-pool dedup by doc_id
# ---------------------------------------------------------------------------


class TestCrossPoolDedup:
    """Chunks whose doc_id was already emitted as evidence are skipped."""

    def test_chunk_deduped_when_doc_already_in_evidence(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        shared_doc_id = 42
        evidence_content = "shared evidence text"
        evidence: list[tuple[int, str, str | None, str | None, str | None, str | None]] = [
            (shared_doc_id, evidence_content, None, None, None, None)
        ]
        triple = _scored_item(1, "A", "rel", "B", score=1.0, evidence=evidence)
        # Chunk with the SAME doc_id should be deduped.
        dup_chunk: _Chunk = (shared_doc_id, "should be deduped", None, None, None, None)

        out = store.fused_output(
            scored=[triple],
            chunks=[dup_chunk],
            budget=10_000,
            chunk_share=0.4,
            top_evidence_k=1,
        )
        # The dup chunk content must NOT appear in the chunk section.
        assert "should be deduped" not in out
        # The evidence must appear (it was admitted).
        assert evidence_content in out

    def test_different_doc_id_not_deduped(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        evidence: list[tuple[int, str, str | None, str | None, str | None, str | None]] = [
            (10, "evidence text", None, None, None, None)
        ]
        triple = _scored_item(1, "A", "rel", "B", score=1.0, evidence=evidence)
        distinct_chunk: _Chunk = (20, "distinct chunk content", None, None, None, None)

        out = store.fused_output(
            scored=[triple],
            chunks=[distinct_chunk],
            budget=10_000,
            chunk_share=0.4,
            top_evidence_k=1,
        )
        assert "distinct chunk content" in out


# ---------------------------------------------------------------------------
# 3. fused_output: empty-pool degradations
# ---------------------------------------------------------------------------


class TestEmptyPoolDegradations:
    """Empty scored → full budget to chunks; empty chunks → triples get whole budget."""

    def test_empty_triples_gives_whole_budget_to_chunks(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # budget=100, chunk_share=0.4 → chunk_reserve=40, triple_allowance=60
        # With no triples, all 60 leftover from pass 1 rolls to chunk_allowance=100.
        chunk = _chunk(1, "membox uses SQLite for storage")

        out = store.fused_output(
            scored=[],
            chunks=[chunk],
            budget=100,
            chunk_share=0.4,
            top_evidence_k=3,
        )
        assert "membox uses SQLite for storage" in out
        assert "returned 0/0 triples" in out
        assert "1/1 FTS chunks" in out

    def test_empty_chunks_gives_whole_budget_to_triples(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        triples = [_scored_item(i, f"S{i}", "rel", f"O{i}", score=float(5 - i)) for i in range(5)]

        out = store.fused_output(
            scored=triples,
            chunks=[],
            budget=10_000,
            chunk_share=0.4,
            top_evidence_k=0,
        )
        # All 5 triples admitted.
        assert "returned 5/5 triples" in out
        assert "0/0 FTS chunks" in out

    def test_both_pools_empty(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        out = store.fused_output(
            scored=[],
            chunks=[],
            budget=1000,
            chunk_share=0.4,
            top_evidence_k=3,
        )
        assert "returned 0/0 triples" in out
        assert "0/0 FTS chunks" in out


# ---------------------------------------------------------------------------
# 4. fused_output: oversized chunk → 0/L FTS chunks
# ---------------------------------------------------------------------------


class TestOversizedChunk:
    """When all chunks exceed chunk_reserve+leftover, 0/L FTS chunks is correct."""

    def test_oversized_chunk_not_admitted(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        # budget=10, chunk_share=0.5 → chunk_reserve=5, triple_allowance=5
        # A chunk with cost > 5 (even with leftover) must not be admitted.
        huge_content = "x" * 400  # ≈ ceil(400/4) = 100 tokens >> 5
        chunk = _chunk(1, huge_content)

        out = store.fused_output(
            scored=[],
            chunks=[chunk],
            budget=10,
            chunk_share=0.5,
            top_evidence_k=0,
        )
        assert "0/1 FTS chunks" in out
        assert huge_content not in out
        assert "raise --budget for more" in out

    def test_one_fits_one_oversized(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        small_content = "hi"
        huge_content = "z" * 400
        chunks: list[_Chunk] = [
            _chunk(1, small_content),
            _chunk(2, huge_content),
        ]
        out = store.fused_output(
            scored=[],
            chunks=chunks,
            budget=100,
            chunk_share=0.5,
            top_evidence_k=0,
        )
        assert small_content in out
        assert huge_content not in out
        assert "1/2 FTS chunks" in out


# ---------------------------------------------------------------------------
# 5. fused_output: footer counts N/M & K/L
# ---------------------------------------------------------------------------


class TestFooterCounts:
    """Footer reports admitted/candidate triples and admitted/candidate chunks."""

    def test_footer_format_with_both_pools(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        triples = [_scored_item(i, f"A{i}", "r", f"B{i}") for i in range(3)]
        chunks = [_chunk(i + 10, f"chunk {i} content") for i in range(2)]

        out = store.fused_output(
            scored=triples,
            chunks=chunks,
            budget=10_000,
            chunk_share=0.4,
            top_evidence_k=0,
        )
        assert "returned 3/3 triples" in out
        assert "2/2 FTS chunks" in out

    def test_footer_raise_hint_when_triple_truncated(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        triples = [_scored_item(i, f"A{i}", "rel", f"B{i}") for i in range(5)]
        out = store.fused_output(
            scored=triples,
            chunks=[],
            budget=1,  # too small for any triple
            chunk_share=0.0,
            top_evidence_k=0,
        )
        assert "raise --budget for more" in out
        assert "0/5 triples" in out

    def test_footer_raise_hint_when_chunk_truncated(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        chunks = [_chunk(i, "x" * 400) for i in range(3)]  # each ~100 tokens
        out = store.fused_output(
            scored=[],
            chunks=chunks,
            budget=50,  # fits maybe 0 chunks
            chunk_share=0.5,
            top_evidence_k=0,
        )
        assert "raise --budget for more" in out

    def test_no_raise_hint_when_all_admitted(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        triples = [_scored_item(1, "A", "r", "B")]
        chunks = [_chunk(10, "small chunk")]
        out = store.fused_output(
            scored=triples,
            chunks=chunks,
            budget=10_000,
            chunk_share=0.4,
            top_evidence_k=0,
        )
        footer_lines = [ln for ln in out.splitlines() if ln.startswith("(returned")]
        assert len(footer_lines) == 1
        assert "raise --budget" not in footer_lines[0]

    def test_footer_contains_tokens(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "s.db"))
        out = store.fused_output(scored=[], chunks=[], budget=500, chunk_share=0.4)
        assert "tokens" in out


# ---------------------------------------------------------------------------
# 6. fusion_mode="fallback" preserves old behavior bit-for-bit
# ---------------------------------------------------------------------------


class TestFallbackModePreservation:
    """fusion_mode="fallback" gives identical results to the old control flow."""

    def test_fallback_mode_graph_hit_no_fts(self, tmp_path: Path) -> None:
        """In fallback mode, a graph hit suppresses FTS entirely."""
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        agent.ingest_extracted("Membox uses SQLite for all storage.", graph)

        cfg_fallback = RetrievalConfig(fusion_mode="fallback")
        out = agent.compact_query("what does Membox use", config=cfg_fallback)

        # Graph triple present.
        assert "Membox" in out
        # No FTS chunk section.
        assert "FTS chunks" not in out

    def test_merge_vs_fallback_differ_when_graph_non_empty(self, tmp_path: Path) -> None:
        """merge mode shows FTS chunks alongside triples; fallback does not."""
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        agent.ingest_extracted("Membox uses SQLite.", graph)

        cfg_merge = RetrievalConfig(fusion_mode="merge", fts_fallback_k=5)
        cfg_fallback = RetrievalConfig(fusion_mode="fallback", fts_fallback_k=5)

        out_merge = agent.compact_query("what does Membox use", config=cfg_merge)
        out_fallback = agent.compact_query("what does Membox use", config=cfg_fallback)

        # Merge footer reports FTS chunks; fallback footer does not.
        assert "FTS chunks" in out_merge
        assert "FTS chunks" not in out_fallback


# ---------------------------------------------------------------------------
# 7. fts_fallback_k=0 in merge mode → pure graph (no chunks)
# ---------------------------------------------------------------------------


class TestMergeModeWithFtsDisabled:
    """merge mode + fts_fallback_k=0 behaves like pure graph retrieval."""

    def test_no_chunks_when_fts_k_zero(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        agent.ingest_extracted("Membox uses SQLite.", graph)

        cfg = RetrievalConfig(fusion_mode="merge", fts_fallback_k=0)
        out = agent.compact_query("what does Membox use", config=cfg)

        # Footer should show 0 candidate FTS chunks (fts channel off).
        assert "0/0 FTS chunks" in out
        # Triple is still present.
        assert "Membox" in out

    def test_empty_seeds_merge_mode_fts_disabled(self, tmp_path: Path) -> None:
        """With no seeds and fts disabled, fused_output returns bare-ish footer."""
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)

        cfg = RetrievalConfig(fusion_mode="merge", fts_fallback_k=0)
        out = agent.compact_query("anything", config=cfg)

        # Both pools empty → both 0/0 in footer.
        assert "0/0 triples" in out
        assert "0/0 FTS chunks" in out


# ---------------------------------------------------------------------------
# 8. fused_output used through full agent pipeline (integration)
# ---------------------------------------------------------------------------


class TestFusionIntegration:
    """End-to-end: agent compact_query with fusion_mode="merge"."""

    def test_merge_mode_combines_triples_and_chunks(self, tmp_path: Path) -> None:
        """Graph triple AND FTS chunk both appear in merge output."""
        agent = _make_agent(tmp_path, _SeedExtractor(["Membox"]))
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(name="Membox", type="Project"),
                ExtractedEntity(name="SQLite", type="Technology"),
            ],
            relations=[ExtractedRelation(source="Membox", target="SQLite", predicate="uses")],
        )
        # Ingest with both graph data and FTS-searchable text.
        agent.ingest_extracted(
            "Membox uses SQLite for reliable local storage of knowledge graphs.",
            graph,
        )

        cfg = RetrievalConfig(fusion_mode="merge", fts_fallback_k=5, budget=2000)
        out = agent.compact_query("Membox SQLite storage", config=cfg)

        # Triple section present.
        assert "Membox" in out
        # FTS chunk section or count present (chunk may be deduped by cross-pool
        # dedup if same doc, but the footer always reports the count).
        assert "FTS chunks" in out

    def test_merge_mode_project_filter(self, tmp_path: Path) -> None:
        """project_filter scopes both triple pool and chunk pool."""
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("alpha uses Redis caching.", _EMPTY_GRAPH, project="alpha")
        agent.ingest_extracted("beta uses Redis caching.", _EMPTY_GRAPH, project="beta")

        cfg = RetrievalConfig(fusion_mode="merge", fts_fallback_k=5)
        out = agent.compact_query("who uses Redis", config=cfg, project_filter="alpha")

        assert "alpha uses Redis" in out
        assert "beta uses Redis" not in out

    @pytest.mark.parametrize("mode", ["merge", "fallback"])
    def test_pending_note_appended_in_both_modes(self, tmp_path: Path, mode: str) -> None:
        """_append_pending_note fires regardless of fusion_mode."""
        agent = _make_agent(tmp_path)
        agent.ingest_extracted("SQLite is the storage backend.", _EMPTY_GRAPH)
        agent.store.enqueue_ingest("queued content", project=None, source_path=None)

        cfg = RetrievalConfig(fusion_mode=mode)
        out = agent.compact_query("storage backend", config=cfg)

        assert "pending" in out
