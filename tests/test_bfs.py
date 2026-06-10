"""Phase 5 tests: multi-hop BFS retrieval via bfs_query and MemoryAgent.retrieve."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from membox.core.store import KnowledgeStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate(store: KnowledgeStore, edges: list[tuple[str, str, str]]) -> None:
    """Insert entities and relations for a list of (source, predicate, target) triples."""
    doc_id = store.insert_document("test document")
    for src, pred, tgt in edges:
        eid_src = store.find_or_create_entity(src, "Thing", "", None)
        eid_tgt = store.find_or_create_entity(tgt, "Thing", "", None)
        store.upsert_relation(eid_src, eid_tgt, pred, doc_id)


# ---------------------------------------------------------------------------
# 1. bfs_query basic behavior
# ---------------------------------------------------------------------------


def test_bfs_query_empty_graph(tmp_path: Path) -> None:
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    eid = store.find_or_create_entity("Lone", "Thing", "", None)
    result = store.bfs_query([eid], max_hops=2)
    assert result.triplets == []
    assert result.documents == []
    assert "Lone" in result.visited_entities


def test_bfs_query_hop0_returns_seeds_only(tmp_path: Path) -> None:
    """max_hops=0 means no expansion — only seed entities visited, no edges traversed."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("A", "links", "B")])
    eid_a = store.find_entity_by_alias("a")
    assert eid_a is not None
    result = store.bfs_query([eid_a], max_hops=0)
    assert result.triplets == []
    assert "A" in result.visited_entities
    assert "B" not in result.visited_entities


def test_bfs_query_hop1_direct_neighbors(tmp_path: Path) -> None:
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("Alice", "works_at", "Acme")])
    eid_alice = store.find_entity_by_alias("alice")
    assert eid_alice is not None
    result = store.bfs_query([eid_alice], max_hops=1)
    assert len(result.triplets) == 1
    assert result.triplets[0] == ("Alice", "works_at", "Acme")
    assert "Alice" in result.visited_entities
    assert "Acme" in result.visited_entities


def test_bfs_query_hop2_transitive(tmp_path: Path) -> None:
    """A → B → C: seeding A with max_hops=2 should reach C."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("A", "links", "B"), ("B", "links", "C")])
    eid_a = store.find_entity_by_alias("a")
    assert eid_a is not None
    result = store.bfs_query([eid_a], max_hops=2)
    names = result.visited_entities
    assert "A" in names
    assert "B" in names
    assert "C" in names
    assert len(result.triplets) == 2


def test_bfs_query_hop1_does_not_reach_hop2(tmp_path: Path) -> None:
    """A → B → C: seeding A with max_hops=1 must NOT reach C."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("A", "links", "B"), ("B", "links", "C")])
    eid_a = store.find_entity_by_alias("a")
    assert eid_a is not None
    result = store.bfs_query([eid_a], max_hops=1)
    assert "C" not in result.visited_entities


def test_bfs_query_cycle_no_infinite_loop(tmp_path: Path) -> None:
    """A → B → A cycle must terminate without revisiting A."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("A", "links", "B"), ("B", "links", "A")])
    eid_a = store.find_entity_by_alias("a")
    assert eid_a is not None
    result = store.bfs_query([eid_a], max_hops=5)
    # Both visited; no duplication
    assert "A" in result.visited_entities
    assert "B" in result.visited_entities
    # Only one edge A→B and one B→A (or single dedup): ≤ 2 triplets
    assert len(result.triplets) <= 2


def test_bfs_query_multiple_seeds(tmp_path: Path) -> None:
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    _populate(store, [("Alice", "leads", "Team"), ("Bob", "member_of", "Team")])
    eid_alice = store.find_entity_by_alias("alice")
    eid_bob = store.find_entity_by_alias("bob")
    assert eid_alice is not None and eid_bob is not None
    result = store.bfs_query([eid_alice, eid_bob], max_hops=1)
    names = result.visited_entities
    assert "Alice" in names
    assert "Bob" in names
    assert "Team" in names


def test_bfs_query_evidence_docs_included(tmp_path: Path) -> None:
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    doc_id = store.insert_document("Alice works at Acme.")
    e1 = store.find_or_create_entity("Alice", "Person", "", None)
    e2 = store.find_or_create_entity("Acme", "Org", "", None)
    store.upsert_relation(e1, e2, "works_at", doc_id)
    result = store.bfs_query([e1], max_hops=1)
    assert "Alice works at Acme." in result.documents


def test_bfs_query_evidence_docs_deduped(tmp_path: Path) -> None:
    """Two relations citing the same doc should produce only one document entry."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "b.db"))
    doc_id = store.insert_document("shared evidence")
    e1 = store.find_or_create_entity("A", "Thing", "", None)
    e2 = store.find_or_create_entity("B", "Thing", "", None)
    e3 = store.find_or_create_entity("C", "Thing", "", None)
    store.upsert_relation(e1, e2, "r1", doc_id)
    store.upsert_relation(e1, e3, "r2", doc_id)
    result = store.bfs_query([e1], max_hops=1)
    assert result.documents.count("shared evidence") == 1


# ---------------------------------------------------------------------------
# 2. MemoryAgent.retrieve end-to-end
# ---------------------------------------------------------------------------


def test_agent_retrieve_resolves_seeds_and_bfs(tmp_path: Path) -> None:
    from membox.core.agent import MemoryAgent
    from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
    from membox.services.extraction import DummyExtractor

    db = str(tmp_path / "a.db")
    agent = MemoryAgent(extractor=DummyExtractor(), db_path=db)
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(name="Alice", type="Person"),
            ExtractedEntity(name="Acme", type="Org"),
        ],
        relations=[ExtractedRelation(source="Alice", target="Acme", predicate="works_at")],
    )
    agent.ingest_extracted("Alice works at Acme.", graph)
    result = agent.retrieve(["Alice"], max_hops=1)
    assert "Alice" in result.visited_entities
    assert "Acme" in result.visited_entities
    assert any(t[1] == "works_at" for t in result.triplets)


def test_agent_retrieve_unresolved_seeds_returns_empty(tmp_path: Path) -> None:
    from membox.core.agent import MemoryAgent
    from membox.services.extraction import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    result = agent.retrieve(["UnknownPerson"], max_hops=2)
    assert result.triplets == []
    assert result.visited_entities == []
    assert result.seed_names == ["UnknownPerson"]


def test_agent_query_returns_formatted_context(tmp_path: Path) -> None:
    """query() with DummyExtractor (no seeds) returns compact coverage footer."""
    from membox.core.agent import MemoryAgent
    from membox.services.extraction import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    ctx = agent.query("What does Alice do?")
    assert "returned" in ctx
    assert "triples" in ctx


# ---------------------------------------------------------------------------
# 3. to_prompt_context formats BFS result
# ---------------------------------------------------------------------------


def test_to_prompt_context_formats_triplets_and_docs(tmp_path: Path) -> None:
    from membox.core.agent import MemoryAgent
    from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
    from membox.services.extraction import DummyExtractor

    db = str(tmp_path / "ctx.db")
    agent = MemoryAgent(extractor=DummyExtractor(), db_path=db)
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(name="Alice", type="Person"),
            ExtractedEntity(name="Acme", type="Org"),
        ],
        relations=[ExtractedRelation(source="Alice", target="Acme", predicate="works_at")],
    )
    agent.ingest_extracted("Alice works at Acme Corp.", graph)
    result = agent.retrieve(["Alice"], max_hops=1)
    ctx = agent.to_prompt_context(result)
    assert "[Alice]" in ctx
    assert "[Acme]" in ctx
    assert "works_at" in ctx
    assert "Alice works at Acme Corp." in ctx
