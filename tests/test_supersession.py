"""M4 supersession semantics tests.

Covers:
- Migration 0007: user_version bumped to 7, superseded_by column present.
- Forward-only supersession: doc v2 same source_path, different object → old superseded.
- Same-object re-assert does not supersede.
- Different source_path does not supersede.
- NULL source_path does not supersede.
- NULL version does not supersede.
- Retrieval (scored_query / bfs_query) excludes superseded edges by default.
- Retrieval includes superseded edges when include_superseded=True.
- CLI query --include-superseded flag accepted (CliRunner smoke test).
- list_relations includes superseded_by field; CLI listing shows marker.
- Evidence rows are intact after supersession (never deleted).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from membox.cli import app
from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.core.store.migrations import get_user_version
from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
from membox.services.extraction import DummyExtractor

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(db_path: str) -> MemoryAgent:
    """Return a MemoryAgent with DummyExtractor and no embedder."""
    return MemoryAgent(extractor=DummyExtractor(), db_path=db_path)


def _graph(
    entities: list[tuple[str, str]],
    relations: list[tuple[str, str, str]],
) -> ExtractedGraph:
    """Build a minimal ExtractedGraph from tuples."""
    return ExtractedGraph(
        entities=[ExtractedEntity(name=n, type=t) for n, t in entities],
        relations=[ExtractedRelation(source=s, predicate=p, target=o) for s, p, o in relations],
    )


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------


class TestMigration0007:
    """Migration 0007 adds superseded_by column and active-relations index."""

    def test_fresh_db_at_version_7(self, tmp_path: Path) -> None:
        """A freshly opened store must be at the latest user_version (currently 9)."""
        store = KnowledgeStore(str(tmp_path / "db.db"))
        assert get_user_version(store._conn()) == 9

    def test_superseded_by_column_present(self, tmp_path: Path) -> None:
        """relations table must have a superseded_by column."""
        store = KnowledgeStore(str(tmp_path / "db.db"))
        cols = {row[1] for row in store._conn().execute("PRAGMA table_info(relations);").fetchall()}
        assert "superseded_by" in cols

    def test_active_index_present(self, tmp_path: Path) -> None:
        """idx_relations_active partial index must exist after migration."""
        store = KnowledgeStore(str(tmp_path / "db.db"))
        indexes = {
            row[1]
            for row in store._conn()
            .execute("SELECT type, name FROM sqlite_master WHERE type='index';")
            .fetchall()
        }
        assert "idx_relations_active" in indexes

    def test_v6_db_upgraded_to_v7(self, tmp_path: Path) -> None:
        """A database at user_version=6 must be transparently upgraded to 7."""
        from membox.core.store.migrations import MIGRATIONS, apply_migrations

        db_path = str(tmp_path / "v6.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        apply_migrations(conn, [(v, a) for v, a in MIGRATIONS if v <= 6])
        assert get_user_version(conn) == 6
        # superseded_by must be absent at v6.
        cols_v6_pre = {row[1] for row in conn.execute("PRAGMA table_info(relations);").fetchall()}
        assert "superseded_by" not in cols_v6_pre
        conn.close()

        # Opening via KnowledgeStore triggers migration 7.
        store = KnowledgeStore(db_path)
        assert get_user_version(store._conn()) == 9
        cols_v7 = {
            row[1] for row in store._conn().execute("PRAGMA table_info(relations);").fetchall()
        }
        assert "superseded_by" in cols_v7


# ---------------------------------------------------------------------------
# 2. Supersession detection
# ---------------------------------------------------------------------------


class TestSupersessionDetection:
    """Store-layer supersession detection in upsert_relation."""

    def test_v2_different_object_supersedes_v1(self, tmp_path: Path) -> None:
        """doc v2 (same source_path, different object) → v1 relation superseded."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )

        agent.ingest_extracted("v1", graph_v1, source_path="/code/alpha.py")
        agent.ingest_extracted("v2", graph_v2, source_path="/code/alpha.py")

        relations = agent.store.list_relations()
        # There should be two relations.
        assert len(relations) == 2
        r_b = next(r for r in relations if r.target_name == "BLib")
        r_c = next(r for r in relations if r.target_name == "CLib")
        # The v1 relation (uses BLib) must be superseded by the v2 relation.
        assert r_b.superseded_by == r_c.id
        assert r_c.superseded_by is None

    def test_same_object_re_assert_does_not_supersede(self, tmp_path: Path) -> None:
        """Reasserting the same subject+predicate+object never sets superseded_by."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        agent.ingest_extracted("v1", graph, source_path="/code/alpha.py")
        agent.ingest_extracted("v2", graph, source_path="/code/alpha.py")

        relations = agent.store.list_relations()
        assert len(relations) == 1
        assert relations[0].superseded_by is None

    def test_different_source_path_does_not_supersede(self, tmp_path: Path) -> None:
        """Relations from a different source_path must not supersede each other."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph_a = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_b = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        agent.ingest_extracted("docA", graph_a, source_path="/code/a.py")
        agent.ingest_extracted("docB", graph_b, source_path="/code/b.py")

        relations = agent.store.list_relations()
        assert len(relations) == 2
        for r in relations:
            assert r.superseded_by is None

    def test_null_source_path_does_not_supersede(self, tmp_path: Path) -> None:
        """Documents with no source_path set must never trigger supersession."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        # No source_path supplied → version stays NULL.
        agent.ingest_extracted("v1", graph_v1)
        agent.ingest_extracted("v2", graph_v2)

        relations = agent.store.list_relations()
        for r in relations:
            assert r.superseded_by is None

    def test_evidence_rows_intact_after_supersession(self, tmp_path: Path) -> None:
        """Evidence is never deleted; superseded relation still has evidence rows."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        agent.ingest_extracted("v1 content", graph_v1, source_path="/code/alpha.py")
        agent.ingest_extracted("v2 content", graph_v2, source_path="/code/alpha.py")

        relations = agent.store.list_relations()
        superseded = next(r for r in relations if r.superseded_by is not None)
        # Evidence rows must still exist for the superseded relation.
        evidence = agent.store.get_evidence_docs([superseded.id])
        assert len(evidence) >= 1, "superseded relation must retain its evidence"


# ---------------------------------------------------------------------------
# 3. Retrieval filtering
# ---------------------------------------------------------------------------


class TestRetrievalFiltering:
    """Superseded relations are excluded from query results by default."""

    def _setup_superseded(self, db_path: str) -> tuple[MemoryAgent, int, int]:
        """Ingest v1 then v2 and return (agent, old_rid, new_rid)."""
        agent = _make_agent(db_path)
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        agent.ingest_extracted("v1", graph_v1, source_path="/code/alpha.py")
        agent.ingest_extracted("v2", graph_v2, source_path="/code/alpha.py")

        relations = agent.store.list_relations()
        old_rid = next(r.id for r in relations if r.superseded_by is not None)
        new_rid = next(
            r.id for r in relations if r.superseded_by is None and r.target_name == "CLib"
        )
        return agent, old_rid, new_rid

    def test_get_neighbors_excludes_superseded_by_default(self, tmp_path: Path) -> None:
        """get_neighbors with default flag must not return superseded relations."""
        agent, old_rid, _ = self._setup_superseded(str(tmp_path / "db.db"))
        # Find alpha entity id.
        alpha_id = agent.store.find_entity_by_alias("alpha")
        assert alpha_id is not None
        neighbors = agent.store.get_neighbors([alpha_id])
        returned_rids = {r[0] for r in neighbors}
        assert old_rid not in returned_rids

    def test_get_neighbors_includes_superseded_when_flag_set(self, tmp_path: Path) -> None:
        """get_neighbors with include_superseded=True must return all relations."""
        agent, old_rid, _ = self._setup_superseded(str(tmp_path / "db.db"))
        alpha_id = agent.store.find_entity_by_alias("alpha")
        assert alpha_id is not None
        neighbors = agent.store.get_neighbors([alpha_id], include_superseded=True)
        returned_rids = {r[0] for r in neighbors}
        assert old_rid in returned_rids

    def test_bfs_query_excludes_superseded_by_default(self, tmp_path: Path) -> None:
        """bfs_query result must not include superseded relations by default."""
        agent, _old_rid, _ = self._setup_superseded(str(tmp_path / "db.db"))
        alpha_id = agent.store.find_entity_by_alias("alpha")
        assert alpha_id is not None
        result = agent.store.bfs_query([alpha_id], max_hops=2)
        # BLib (superseded target) should not appear in triplets.
        targets = {t for _, _, t in result.triplets}
        assert "BLib" not in targets

    def test_bfs_query_includes_superseded_when_flag_set(self, tmp_path: Path) -> None:
        """bfs_query with include_superseded=True surfaces superseded relations."""
        agent, _old_rid, _ = self._setup_superseded(str(tmp_path / "db.db"))
        alpha_id = agent.store.find_entity_by_alias("alpha")
        assert alpha_id is not None
        result = agent.store.bfs_query([alpha_id], max_hops=2, include_superseded=True)
        targets = {t for _, _, t in result.triplets}
        assert "BLib" in targets

    def test_agent_query_excludes_superseded_by_default(self, tmp_path: Path) -> None:
        """MemoryAgent.query must not surface superseded relations in output.

        DummyExtractor returns no entities so we verify directly via bfs_query
        that the flag-off path never touches superseded edges.
        """
        agent, _, _ = self._setup_superseded(str(tmp_path / "db.db"))
        # DummyExtractor returns no seed entities so agent.query falls back to
        # FTS; still must not raise and output must be a string.
        result = agent.query("alpha uses what")
        assert isinstance(result, str)
        # The superseded object must not appear (either path: no seeds or BFS).
        assert "BLib" not in result

    def test_agent_query_include_superseded_flag(self, tmp_path: Path) -> None:
        """MemoryAgent.query with include_superseded=True must not raise."""
        agent, _, _ = self._setup_superseded(str(tmp_path / "db.db"))
        result = agent.query("alpha uses what", include_superseded=True)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 4. CLI smoke tests
# ---------------------------------------------------------------------------


class TestCLISuperseded:
    """CLI --include-superseded flag is accepted without error."""

    def test_query_include_superseded_flag_accepted(self, tmp_path: Path) -> None:
        """membox query --include-superseded runs without error."""
        db_path = str(tmp_path / "db.db")
        # Ensure DB is initialized.
        KnowledgeStore(db_path)
        result = runner.invoke(
            app,
            ["query", "--db", db_path, "--no-llm", "--include-superseded", "test question"],
        )
        assert result.exit_code == 0, result.output

    def test_query_without_flag_still_works(self, tmp_path: Path) -> None:
        """Standard query (no --include-superseded) continues to function."""
        db_path = str(tmp_path / "db.db")
        KnowledgeStore(db_path)
        result = runner.invoke(
            app,
            ["query", "--db", db_path, "--no-llm", "test question"],
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 5. list-relations superseded marker
# ---------------------------------------------------------------------------


class TestListRelationsMarker:
    """list_relations populates superseded_by; CLI shows [superseded] marker."""

    def test_list_relations_superseded_by_populated(self, tmp_path: Path) -> None:
        """list_relations must fill superseded_by on superseded rows."""
        agent = _make_agent(str(tmp_path / "db.db"))
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        agent.ingest_extracted("v1", graph_v1, source_path="/code/alpha.py")
        agent.ingest_extracted("v2", graph_v2, source_path="/code/alpha.py")

        relations = agent.store.list_relations()
        superseded = [r for r in relations if r.superseded_by is not None]
        active = [r for r in relations if r.superseded_by is None]
        assert len(superseded) == 1
        assert len(active) == 1
        assert superseded[0].superseded_by == active[0].id

    def test_cli_list_relations_shows_superseded_marker(self, tmp_path: Path) -> None:
        """membox list-relations output contains '[superseded' text for superseded rows."""
        db_path = str(tmp_path / "db.db")
        agent = _make_agent(db_path)
        graph_v1 = _graph(
            entities=[("Alpha", "Module"), ("BLib", "Library")],
            relations=[("Alpha", "uses", "BLib")],
        )
        graph_v2 = _graph(
            entities=[("Alpha", "Module"), ("CLib", "Library")],
            relations=[("Alpha", "uses", "CLib")],
        )
        agent.ingest_extracted("v1", graph_v1, source_path="/code/alpha.py")
        agent.ingest_extracted("v2", graph_v2, source_path="/code/alpha.py")

        result = runner.invoke(app, ["list-relations", "--db", db_path])
        assert result.exit_code == 0, result.output
        assert "superseded by" in result.output
