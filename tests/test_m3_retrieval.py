"""Phase 7.5 M3 — tests for hybrid retrieval, scoring, knapsack, compact output.

Coverage targets:
- est_tokens: CJK / non-CJK / mixed strings
- BM25 scoring: sign negation, degenerate all-same, no-FTS-match → 0
- Tie-breaking: hops asc then newest date desc
- Knapsack: triple admission, evidence eligibility (top-K + admitted parent),
  skip-and-continue (skip expensive item but continue)
- Compact output: subject-grouped format, footer always present
- Migration v2→v3: existing DB gains embedding column + FTS5 table + triggers
- Meta guard: mismatch raises EmbedderMismatchError; no embedder = no-op
- Config defaults: RetrievalConfig field values
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> object:
    """Create a fresh KnowledgeStore at tmp_path/test.db."""
    from membox.core.store import KnowledgeStore

    return KnowledgeStore(str(tmp_path / "test.db"))


def _make_embedder(dim: int = 4, model: str = "test-model") -> object:
    """Return a simple deterministic embedder for testing."""

    class _E:
        def __init__(self) -> None:
            self.dim = dim
            self.model = model
            self._counter = 0

        def embed(self, text: str) -> list[float]:
            # Return a simple vector based on text length for variety.
            base = [float(len(text) % (i + 2)) for i in range(self.dim)]
            norm = sum(x * x for x in base) ** 0.5 or 1.0
            return [x / norm for x in base]

    return _E()


# ---------------------------------------------------------------------------
# 1. est_tokens
# ---------------------------------------------------------------------------


class TestEstTokens:
    """Token estimator: CJK chars count 1 each; non-CJK = ceil(count/4)."""

    def test_empty(self) -> None:
        from membox.core.store.retrieval import est_tokens

        assert est_tokens("") == 0

    def test_pure_ascii(self) -> None:
        from membox.core.store.retrieval import est_tokens

        # "hello" = 5 chars → ceil(5/4) = 2
        assert est_tokens("hello") == 2

    def test_pure_ascii_exact_multiple(self) -> None:
        from membox.core.store.retrieval import est_tokens

        # "abcd" = 4 chars → ceil(4/4) = 1
        assert est_tokens("abcd") == 1

    def test_pure_cjk(self) -> None:
        from membox.core.store.retrieval import est_tokens

        s = "你好世界"  # 4 CJK characters
        assert est_tokens(s) == 4

    def test_mixed_cjk_and_ascii(self) -> None:
        from membox.core.store.retrieval import est_tokens

        # "AB你好" → 2 CJK + 2 non-CJK → 2 + ceil(2/4) = 2 + 1 = 3
        assert est_tokens("AB你好") == 3

    def test_single_char(self) -> None:
        from membox.core.store.retrieval import est_tokens

        assert est_tokens("a") == 1

    def test_spaces_count_as_non_cjk(self) -> None:
        from membox.core.store.retrieval import est_tokens

        # 4 spaces → ceil(4/4) = 1
        assert est_tokens("    ") == 1


# ---------------------------------------------------------------------------
# 2. BM25 sign + degenerate case
# ---------------------------------------------------------------------------


class TestBM25Scoring:
    """BM25 scoring: negation, degenerate, no-match → 0."""

    def test_bm25_score_nonnegative(self, tmp_path: Path) -> None:
        """All normalised BM25 scores are in [0, 1]."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "bm25.db"))
        doc1 = store.insert_document(
            "Alice uses Python and SQLite", source="d1", project="p", source_path="d1"
        )
        doc2 = store.insert_document(
            "Bob works with Go and Postgres", source="d2", project="p", source_path="d2"
        )
        e1 = store.create_entity("Alice", "Person", "", None)
        e2 = store.create_entity("Python", "Technology", "", None)
        e3 = store.create_entity("Bob", "Person", "", None)
        r1 = store.upsert_relation(e1, e2, "uses", doc1)
        r2 = store.upsert_relation(e3, e2, "works_with", doc2)

        scores = store._bm25_scores_for_relations([r1, r2], "Alice Python")
        for v in scores.values():
            assert 0.0 <= v <= 1.0, f"BM25 score out of range: {v}"

    def test_bm25_degenerate_returns_zero(self, tmp_path: Path) -> None:
        """When all candidates share the same BM25 score, all get 0.0."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "deg.db"))
        # Two docs with identical content → same BM25 score → degenerate.
        same_text = "membox uses sqlite"
        doc1 = store.insert_document(same_text, source="d1", project="p", source_path="d1")
        doc2 = store.insert_document(same_text, source="d2", project="p", source_path="d2")
        e1 = store.create_entity("membox", "Project", "", None)
        e2 = store.create_entity("sqlite", "Technology", "", None)
        r1 = store.upsert_relation(e1, e2, "uses", doc1)
        r2 = store.upsert_relation(e1, e2, "stores_in", doc2)

        scores = store._bm25_scores_for_relations([r1, r2], "membox sqlite")
        for v in scores.values():
            assert v == 0.0, f"Expected 0.0 for degenerate, got {v}"

    def test_bm25_no_match_returns_zero(self, tmp_path: Path) -> None:
        """A relation with no FTS match gets score 0 (not in returned dict)."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "nomatch.db"))
        doc = store.insert_document(
            "Python is a language", source="d", project="p", source_path="d"
        )
        e1 = store.create_entity("Python", "Technology", "", None)
        e2 = store.create_entity("Language", "Concept", "", None)
        rid = store.upsert_relation(e1, e2, "is_a", doc)

        scores = store._bm25_scores_for_relations([rid], "completely unrelated topic xyzzy")
        assert scores.get(rid, 0.0) == 0.0

    def test_bm25_empty_query_returns_empty(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "eq.db"))
        doc = store.insert_document("something", source="d", project="p", source_path="d")
        e1 = store.create_entity("A", "T", "", None)
        e2 = store.create_entity("B", "T", "", None)
        rid = store.upsert_relation(e1, e2, "rel", doc)

        assert store._bm25_scores_for_relations([rid], "") == {}
        assert store._bm25_scores_for_relations([rid], "   ") == {}


# ---------------------------------------------------------------------------
# 3. Hops computation + tie-breaking
# ---------------------------------------------------------------------------


class TestScoredQuery:
    """scored_query: hops(t) = min(depth(subj), depth(obj)); tie-break order."""

    def _build_chain(self, store: object, n: int) -> tuple[list[int], list[int]]:
        """Build a linear entity chain A→B→C…→N and return (entity_ids, relation_ids)."""
        from membox.core.store import KnowledgeStore

        assert isinstance(store, KnowledgeStore)
        doc = store.insert_document("chain doc", source="d", project="p", source_path="d")
        eids = [store.create_entity(chr(65 + i), "T", "", None) for i in range(n)]
        rids = [store.upsert_relation(eids[i], eids[i + 1], "next", doc) for i in range(n - 1)]
        return eids, rids

    def test_seed_entity_hops_is_zero(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "h0.db"))
        eids, _rids = self._build_chain(store, 3)
        # Seed = first entity; first relation is A→B, hops = min(0,1) = 0.
        results = store.scored_query([eids[0]], 2, "query", None)
        assert any(int(r["hops"]) == 0 for r in results)  # type: ignore[call-overload]

    def test_two_hop_relation_hops_is_one(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "h1.db"))
        eids, _rids = self._build_chain(store, 3)
        # Seed = A (depth 0); B (depth 1), C (depth 2).
        # B→C: hops = min(1, 2) = 1.
        results = store.scored_query([eids[0]], 2, "query", None)
        hops_set = {int(r["hops"]) for r in results}  # type: ignore[call-overload]
        assert 1 in hops_set

    def test_no_seeds_returns_empty(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "ne.db"))
        assert store.scored_query([], 2, "q", None) == []

    def test_tie_break_hops_asc(self, tmp_path: Path) -> None:
        """When scores are equal, lower hops comes first."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "tb.db"))
        eids, _ = self._build_chain(store, 4)
        results = store.scored_query([eids[0]], 3, "something", None)
        # All scores zero (no BM25 match, no embedder). Tie-break by hops asc.
        hops_list = [int(r["hops"]) for r in results]  # type: ignore[call-overload]
        assert hops_list == sorted(hops_list)

    def test_project_filter_excludes_other_projects(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "pf.db"))
        doc_a = store.insert_document(
            "alpha content", source="da", project="alpha", source_path="da"
        )
        doc_b = store.insert_document("beta content", source="db", project="beta", source_path="db")
        e1 = store.create_entity("X", "T", "", None)
        e2 = store.create_entity("Y", "T", "", None)
        e3 = store.create_entity("Z", "T", "", None)
        store.upsert_relation(e1, e2, "rel_ab", doc_a)
        store.upsert_relation(e1, e3, "rel_b", doc_b)

        results = store.scored_query([e1], 1, "q", None, project_filter="alpha")
        objects = {r["object"] for r in results}
        # rel_b (only beta evidence) should be excluded.
        assert "Z" not in objects


# ---------------------------------------------------------------------------
# 4. Knapsack eligibility
# ---------------------------------------------------------------------------


class TestKnapsack:
    """Knapsack: skip-and-continue, evidence top-K gate, evidence admitted-parent gate."""

    def _store_with_triples(
        self,
        tmp_path: Path,
        n: int = 10,
    ) -> object:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "ks.db"))
        doc = store.insert_document(
            "base content membox python sqlite",
            source="d",
            project="p",
            source_path="d",
        )
        e1 = store.create_entity("Root", "T", "", None)
        for i in range(n):
            e_other = store.create_entity(f"Node{i}", "T", "", None)
            store.upsert_relation(e1, e_other, f"rel{i}", doc)
        return store

    def test_all_triples_fit_large_budget(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = self._store_with_triples(tmp_path)
        assert isinstance(store, KnowledgeStore)
        e_root = store.find_entity_by_alias("root")
        assert e_root is not None
        scored = store.scored_query([e_root], 1, "membox", None)
        output = store.compact_output(scored, budget=100_000)
        # All 10 triples should appear.
        assert "returned 10/10" in output

    def test_small_budget_truncates(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = self._store_with_triples(tmp_path)
        assert isinstance(store, KnowledgeStore)
        e_root = store.find_entity_by_alias("root")
        assert e_root is not None
        scored = store.scored_query([e_root], 1, "membox", None)
        # Budget of 1 token: essentially nothing fits.
        output = store.compact_output(scored, budget=1)
        assert "returned 0/10" in output

    def test_footer_always_present(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = self._store_with_triples(tmp_path)
        assert isinstance(store, KnowledgeStore)
        e_root = store.find_entity_by_alias("root")
        assert e_root is not None
        scored = store.scored_query([e_root], 1, "q", None)
        for budget in [0, 1, 50, 100_000]:
            out = store.compact_output(scored, budget=budget)
            assert "returned" in out and "triples" in out, f"Footer missing for budget={budget}"

    def test_evidence_only_for_top_k_admitted(self, tmp_path: Path) -> None:
        """Evidence snippets only appear for top-K triples that were admitted."""
        from membox.core.store import KnowledgeStore

        store = self._store_with_triples(tmp_path)
        assert isinstance(store, KnowledgeStore)
        e_root = store.find_entity_by_alias("root")
        assert e_root is not None
        scored = store.scored_query([e_root], 1, "membox python sqlite", None)
        # top_evidence_k=1: only 1 snippet eligible.
        output = store.compact_output(scored, budget=100_000, top_evidence_k=1)
        # Count evidence blocks: look for lines starting with "[".
        evidence_lines = [line for line in output.splitlines() if line.startswith("[")]
        # At most 1 evidence block (k=1).
        assert len(evidence_lines) <= 1

    def test_skip_and_continue_not_stop(self, tmp_path: Path) -> None:
        """Expensive item is skipped but cheaper items after it are still admitted.

        We build a custom scored list where an expensive item appears first (by
        manipulating scores directly) and a cheap item appears second, then
        verify the cheap item is admitted despite the budget not covering both.
        """
        from membox.core.store import KnowledgeStore
        from membox.core.store.retrieval import est_tokens

        store = KnowledgeStore(str(tmp_path / "snc.db"))
        doc = store.insert_document("content", source="d", project="p", source_path="d")
        e_seed = store.create_entity("Seed", "T", "", None)
        e_long = store.create_entity("A" * 200, "T", "", None)
        e_short = store.create_entity("B", "T", "", None)
        store.upsert_relation(e_seed, e_long, "x", doc)
        store.upsert_relation(e_seed, e_short, "y", doc)

        # Build scored list manually with expensive first, cheap second.
        short_line = "Seed: y B"
        short_cost = est_tokens(short_line)

        # Construct a synthetic scored list: expensive item first (score 1.0),
        # cheap item second (score 0.9).
        scored: list[dict[str, object]] = [
            {
                "relation_id": 999,
                "subject": "Seed",
                "predicate": "x",
                "object": "A" * 200,
                "hops": 0,
                "score": 1.0,
                "sim": 0.0,
                "bm25": 1.0,
                "evidence": [],
                "_best_date": "",
            },
            {
                "relation_id": 998,
                "subject": "Seed",
                "predicate": "y",
                "object": "B",
                "hops": 0,
                "score": 0.9,
                "sim": 0.0,
                "bm25": 0.9,
                "evidence": [],
                "_best_date": "",
            },
        ]
        # Budget: enough for cheap (score 0.9) but NOT for expensive (score 1.0).
        budget = short_cost  # exactly fits cheap item, not expensive

        output = store.compact_output(scored, budget=budget)
        # The cheap item (B) must be admitted even though expensive was skipped.
        assert "B" in output, f"B not found in output:\n{output}"

    def test_evidence_not_admitted_for_non_admitted_triple(self, tmp_path: Path) -> None:
        """Evidence for a triple not admitted (budget exceeded) is not shown."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "na.db"))
        doc = store.insert_document(
            "long evidence text " * 50, source="d", project="p", source_path="d"
        )
        e1 = store.create_entity("A", "T", "", None)
        e2 = store.create_entity("B", "T", "", None)
        store.upsert_relation(e1, e2, "rel", doc)

        scored = store.scored_query([e1], 1, "q", None)
        # Budget = 0: triple not admitted.
        output = store.compact_output(scored, budget=0, top_evidence_k=1)
        # Evidence text should not appear.
        assert "long evidence text" not in output


# ---------------------------------------------------------------------------
# 5. Compact output format
# ---------------------------------------------------------------------------


class TestCompactOutput:
    """Compact output: subject-grouped, predicates in score order, footer."""

    def test_subject_grouped(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "sg.db"))
        doc = store.insert_document("c", source="d", project="p", source_path="d")
        e1 = store.create_entity("Alice", "Person", "", None)
        e2 = store.create_entity("Python", "Tech", "", None)
        e3 = store.create_entity("SQLite", "Tech", "", None)
        store.upsert_relation(e1, e2, "uses", doc)
        store.upsert_relation(e1, e3, "prefers", doc)

        scored = store.scored_query([e1], 1, "Alice", None)
        output = store.compact_output(scored, budget=10_000)
        # Both predicates should appear on the same line as Alice.
        alice_lines = [line for line in output.splitlines() if line.startswith("Alice:")]
        assert len(alice_lines) == 1, f"Expected one Alice line, got: {alice_lines}"
        assert "|" in alice_lines[0]  # multiple predicates joined by |

    def test_footer_format(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "ff.db"))
        doc = store.insert_document("c", source="d", project="p", source_path="d")
        e1 = store.create_entity("X", "T", "", None)
        e2 = store.create_entity("Y", "T", "", None)
        store.upsert_relation(e1, e2, "r", doc)

        scored = store.scored_query([e1], 1, "q", None)
        output = store.compact_output(scored, budget=10_000)
        footer = [line for line in output.splitlines() if line.startswith("(returned")]
        assert len(footer) == 1
        assert "triples" in footer[0]
        assert "tokens" in footer[0]

    def test_raise_budget_hint_when_truncated(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "rh.db"))
        doc = store.insert_document("c", source="d", project="p", source_path="d")
        e1 = store.create_entity("X", "T", "", None)
        for i in range(5):
            ei = store.create_entity(f"Y{i}", "T", "", None)
            store.upsert_relation(e1, ei, f"r{i}", doc)

        scored = store.scored_query([e1], 1, "q", None)
        output = store.compact_output(scored, budget=1)  # tiny budget → truncation
        assert "--budget" in output

    def test_no_raise_hint_when_all_admitted(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "nh.db"))
        doc = store.insert_document("c", source="d", project="p", source_path="d")
        e1 = store.create_entity("X", "T", "", None)
        e2 = store.create_entity("Y", "T", "", None)
        store.upsert_relation(e1, e2, "r", doc)

        scored = store.scored_query([e1], 1, "q", None)
        output = store.compact_output(scored, budget=100_000)
        footer = [line for line in output.splitlines() if line.startswith("(returned")]
        assert "--budget" not in footer[0]

    def test_provenance_tag_in_evidence(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "pt.db"))
        doc = store.insert_document(
            "python is great",
            source="notes.md",
            project="myproject",
            source_path="notes.md",
            section="Intro",
            doc_date="2026-06-01",
        )
        e1 = store.create_entity("Python", "Tech", "", None)
        e2 = store.create_entity("Great", "Concept", "", None)
        store.upsert_relation(e1, e2, "is", doc)

        scored = store.scored_query([e1], 1, "python", None)
        output = store.compact_output(scored, budget=100_000, top_evidence_k=3)
        assert "myproject" in output
        assert "notes.md" in output


# ---------------------------------------------------------------------------
# 6. Migration v2→v3
# ---------------------------------------------------------------------------


class TestMigrationV3:
    """Migration 0003 adds embedding column to relations + FTS5 + triggers."""

    def test_v2_db_upgraded_to_v3(self, tmp_path: Path) -> None:
        from membox.core.store.migrations import MIGRATIONS, apply_migrations, get_user_version

        db_path = str(tmp_path / "v2tov3.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        # Apply only migrations 1+2 (simulate a v2 database).
        apply_migrations(conn, [(v, a) for v, a in MIGRATIONS if v <= 2])
        assert get_user_version(conn) == 2

        # Insert some existing data before migration 3.
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute("INSERT INTO documents(content, source) VALUES ('old doc', 'src');")
        conn.execute("COMMIT;")

        # Now apply migration 3.
        apply_migrations(conn, [(v, a) for v, a in MIGRATIONS if v == 3])
        assert get_user_version(conn) == 3

        # relations table should have embedding column.
        rel_cols = {row[1] for row in conn.execute("PRAGMA table_info(relations);").fetchall()}
        assert "embedding" in rel_cols

        # documents_fts table should exist.
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        assert "documents_fts" in tables

        conn.close()

    def test_fts_triggers_fire_on_insert(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "fts_trig.db"))
        store.insert_document(
            "unique_keyword_xyzzy_for_fts_test",
            source="t",
            project="p",
            source_path="t",
        )
        conn = store._conn()
        # Search via FTS5 should find the doc.
        rows = conn.execute(
            "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?;",
            ('"unique_keyword_xyzzy_for_fts_test"',),
        ).fetchall()
        assert rows, "FTS5 trigger did not index the inserted document"

    def test_relation_embedding_stored_and_retrieved(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "re.db"))
        doc = store.insert_document("c", source="d", project="p", source_path="d")
        e1 = store.create_entity("A", "T", "", None)
        e2 = store.create_entity("B", "T", "", None)
        embedding = [0.1, 0.2, 0.3, 0.4]
        rid = store.upsert_relation(e1, e2, "rel", doc, embedding=embedding)
        retrieved = store.get_relation_embedding(rid)
        assert retrieved is not None
        assert len(retrieved) == 4
        # Values should match within float32 precision.
        for a, b in zip(embedding, retrieved, strict=True):
            assert abs(a - b) < 1e-5

    def test_backfill_existing_docs_indexed(self, tmp_path: Path) -> None:
        """Docs inserted before migration 3 are backfilled into FTS5."""
        import sqlite3 as _sqlite3

        from membox.core.store.migrations import MIGRATIONS, apply_migrations

        db_path = str(tmp_path / "backfill.db")
        conn = _sqlite3.connect(db_path, isolation_level=None)
        # v1+v2 only.
        apply_migrations(conn, [(v, a) for v, a in MIGRATIONS if v <= 2])
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "INSERT INTO documents(content, source) VALUES ('backfill_unique_word_abc', 'src');"
        )
        conn.execute("COMMIT;")

        # Upgrade to v3.
        apply_migrations(conn, [(v, a) for v, a in MIGRATIONS if v == 3])
        rows = conn.execute(
            "SELECT rowid FROM documents_fts WHERE documents_fts MATCH '\"backfill_unique_word_abc\"';"
        ).fetchall()
        assert rows, "Migration v3 backfill did not index pre-existing document"
        conn.close()


# ---------------------------------------------------------------------------
# 7. Meta guard
# ---------------------------------------------------------------------------


class TestMetaGuard:
    """Embedder meta guard: mismatch raises; no-op without embedder."""

    def test_no_embedder_no_error(self, tmp_path: Path) -> None:
        """Opening a store without embedder is always fine."""
        from membox.core.store import KnowledgeStore

        store = KnowledgeStore(str(tmp_path / "ng.db"))
        assert store is not None  # no exception raised

    def test_first_open_with_embedder_records_meta(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore

        embedder = _make_embedder(dim=8, model="model-a")
        store = KnowledgeStore(str(tmp_path / "meta1.db"), embedder=embedder)  # type: ignore[arg-type]
        conn = store._conn()
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'embedding%';"
            ).fetchall()
        }
        assert rows.get("embedding_model") == "model-a"
        assert rows.get("embedding_dimensions") == "8"

    def test_same_embedder_no_error(self, tmp_path: Path) -> None:
        embedder = _make_embedder(dim=8, model="model-a")
        from membox.core.store import KnowledgeStore

        # First open.
        KnowledgeStore(str(tmp_path / "meta2.db"), embedder=embedder)  # type: ignore[arg-type]
        # Second open with same model/dim.
        KnowledgeStore(str(tmp_path / "meta2.db"), embedder=embedder)  # type: ignore[arg-type]

    def test_different_model_raises(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore
        from membox.core.store.meta_guard import EmbedderMismatchError

        embedder_a = _make_embedder(dim=8, model="model-a")
        embedder_b = _make_embedder(dim=8, model="model-b")
        KnowledgeStore(str(tmp_path / "meta3.db"), embedder=embedder_a)  # type: ignore[arg-type]
        with pytest.raises(EmbedderMismatchError, match="model-a"):
            KnowledgeStore(str(tmp_path / "meta3.db"), embedder=embedder_b)  # type: ignore[arg-type]

    def test_different_dimensions_raises(self, tmp_path: Path) -> None:
        from membox.core.store import KnowledgeStore
        from membox.core.store.meta_guard import EmbedderMismatchError

        embedder_a = _make_embedder(dim=4, model="model-x")
        embedder_b = _make_embedder(dim=8, model="model-x")  # same name, different dim
        KnowledgeStore(str(tmp_path / "meta4.db"), embedder=embedder_a)  # type: ignore[arg-type]
        with pytest.raises(EmbedderMismatchError, match="embedding_dimensions"):
            KnowledgeStore(str(tmp_path / "meta4.db"), embedder=embedder_b)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. Config defaults
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    """RetrievalConfig defaults match spec §3.7."""

    def test_hop_decay(self) -> None:
        from membox.config import RetrievalConfig

        assert RetrievalConfig().hop_decay == 0.7

    def test_alpha(self) -> None:
        from membox.config import RetrievalConfig

        assert RetrievalConfig().alpha == 0.6

    def test_budget(self) -> None:
        from membox.config import RetrievalConfig

        assert RetrievalConfig().budget == 2000

    def test_top_evidence_k(self) -> None:
        from membox.config import RetrievalConfig

        assert RetrievalConfig().top_evidence_k == 3

    def test_disambiguation_threshold(self) -> None:
        from membox.config import RetrievalConfig

        assert RetrievalConfig().disambiguation_threshold == 0.85

    def test_membox_config_has_retrieval(self) -> None:
        from membox.config import MemboxConfig, RetrievalConfig

        cfg = MemboxConfig()
        assert isinstance(cfg.retrieval, RetrievalConfig)


# ---------------------------------------------------------------------------
# 9. Offline eval smoke test
# ---------------------------------------------------------------------------


class TestEvalOffline:
    """Smoke test: eval_memory.py --offline runs end-to-end without crash."""

    @pytest.mark.slow
    def test_offline_eval_runs(self, tmp_path: Path) -> None:
        """Full offline pipeline: ingest corpus → scored_query → compact_output."""
        import importlib.util

        # Locate the eval_memory script.
        script_path = Path(__file__).parent.parent / "scripts" / "eval_memory.py"
        if not script_path.exists():
            pytest.skip("eval_memory.py not found")

        spec = importlib.util.spec_from_file_location("eval_memory", script_path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        corpus_dir = Path(__file__).parent.parent / "eval" / "corpus"
        gold_path = Path(__file__).parent.parent / "eval" / "gold.yaml"
        if not corpus_dir.is_dir() or not gold_path.exists():
            pytest.skip("eval corpus or gold.yaml not found")

        import yaml

        with open(gold_path) as f:
            gold = yaml.safe_load(f) or []

        db_path = str(tmp_path / "eval_smoke.db")
        agent = mod.make_eval_agent(offline=True, db_path=db_path)
        chunks = mod.ingest_corpus(agent, corpus_dir)
        assert chunks > 0

        # Should not raise; hit rate not checked in offline mode.
        exit_code = mod.run_evaluation(agent, gold, budget=2000, check_gates=False, offline=True)
        assert exit_code == 0
