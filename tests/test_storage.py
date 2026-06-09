"""Phase 2 storage tests: SQLite DDL, CRUD, FK constraints, dedup, and evidence lineage."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Schema DDL
# ---------------------------------------------------------------------------


def test_tables_created_on_init(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "ddl.db"))
    conn = store._conn()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }
    expected = {"documents", "entities", "entity_aliases", "relations", "relation_evidence"}
    assert expected.issubset(tables)


def test_wal_mode_enabled(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "wal.db"))
    mode = store._conn().execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode == "wal"


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "fk.db"))
    fk = store._conn().execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1


# ---------------------------------------------------------------------------
# 2. Document CRUD
# ---------------------------------------------------------------------------


def test_insert_document_returns_id(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    doc_id = store.insert_document("hello world", source="test")
    assert isinstance(doc_id, int)
    assert doc_id > 0


def test_insert_document_persists_content(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    store.insert_document("Alice works at Acme.", source="memo.txt")
    row = store._conn().execute("SELECT content, source FROM documents WHERE id=1").fetchone()
    assert row[0] == "Alice works at Acme."
    assert row[1] == "memo.txt"


def test_insert_multiple_documents_unique_ids(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    id1 = store.insert_document("doc one")
    id2 = store.insert_document("doc two")
    assert id1 != id2


# ---------------------------------------------------------------------------
# 3. Entity CRUD
# ---------------------------------------------------------------------------


def test_create_entity_returns_id(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    eid = store.create_entity("Alice", "Person", "Software engineer", None)
    assert isinstance(eid, int)
    assert eid > 0


def test_create_entity_also_registers_canonical_alias(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    eid = store.create_entity("Alice Smith", "Person", "", None)
    found = store.find_entity_by_alias("alice smith")
    assert found == eid


def test_find_entity_by_alias_returns_none_for_unknown(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    assert store.find_entity_by_alias("nobody") is None


def test_get_entity_returns_tuple(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    eid = store.create_entity("Bob", "Person", "Backend dev", None)
    row = store.get_entity(eid)
    assert row is not None
    assert row[0] == eid
    assert row[1] == "Bob"
    assert row[2] == "Person"


def test_get_entity_returns_none_for_missing(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    assert store.get_entity(9999) is None


# ---------------------------------------------------------------------------
# 4. Alias CRUD
# ---------------------------------------------------------------------------


def test_add_alias_registers_additional_alias(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "a.db"))
    eid = store.create_entity("Alice", "Person", "", None)
    store.add_alias("al", eid)
    assert store.find_entity_by_alias("al") == eid


def test_add_alias_idempotent(tmp_path: Path) -> None:
    """Duplicate alias insert should not raise."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "a.db"))
    eid = store.create_entity("Alice", "Person", "", None)
    store.add_alias("al", eid)
    store.add_alias("al", eid)  # idempotent — OR IGNORE


def test_alias_fk_constraint_on_missing_entity(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "a.db"))
    with pytest.raises(sqlite3.IntegrityError):
        store._conn().execute(
            "INSERT INTO entity_aliases(alias, entity_id) VALUES (?, ?)", ("ghost", 9999)
        )


# ---------------------------------------------------------------------------
# 5. Relation CRUD & dedup
# ---------------------------------------------------------------------------


def test_upsert_relation_returns_id(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "r.db"))
    doc_id = store.insert_document("text")
    eid1 = store.create_entity("Alice", "Person", "", None)
    eid2 = store.create_entity("Acme", "Org", "", None)
    rid = store.upsert_relation(eid1, eid2, "works_at", doc_id)
    assert isinstance(rid, int)
    assert rid > 0


def test_upsert_relation_dedup_same_triple(tmp_path: Path) -> None:
    """Same (source, target, predicate) triple should reuse the existing row."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "r.db"))
    doc1 = store.insert_document("doc1")
    doc2 = store.insert_document("doc2")
    eid1 = store.create_entity("Alice", "Person", "", None)
    eid2 = store.create_entity("Acme", "Org", "", None)
    rid1 = store.upsert_relation(eid1, eid2, "works_at", doc1)
    rid2 = store.upsert_relation(eid1, eid2, "works_at", doc2)
    assert rid1 == rid2


def test_upsert_relation_different_predicate_creates_new_row(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "r.db"))
    doc = store.insert_document("doc")
    e1 = store.create_entity("Alice", "Person", "", None)
    e2 = store.create_entity("Acme", "Org", "", None)
    rid1 = store.upsert_relation(e1, e2, "works_at", doc)
    rid2 = store.upsert_relation(e1, e2, "founded", doc)
    assert rid1 != rid2


def test_upsert_relation_fk_on_missing_entity(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "r.db"))
    doc = store.insert_document("doc")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_relation(9999, 8888, "uses", doc)


# ---------------------------------------------------------------------------
# 6. Evidence many-to-many
# ---------------------------------------------------------------------------


def test_evidence_links_relation_to_document(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "ev.db"))
    doc_id = store.insert_document("Alice works at Acme.")
    e1 = store.create_entity("Alice", "Person", "", None)
    e2 = store.create_entity("Acme", "Org", "", None)
    rid = store.upsert_relation(e1, e2, "works_at", doc_id)
    evidence = store.get_evidence_docs([rid])
    assert len(evidence) == 1
    assert evidence[0][0] == rid
    assert evidence[0][2] == "Alice works at Acme."


def test_evidence_accumulates_multiple_docs(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "ev.db"))
    d1 = store.insert_document("doc one")
    d2 = store.insert_document("doc two")
    e1 = store.create_entity("Alice", "Person", "", None)
    e2 = store.create_entity("Acme", "Org", "", None)
    rid = store.upsert_relation(e1, e2, "works_at", d1)
    store.upsert_relation(e1, e2, "works_at", d2)  # same relation, second doc
    evidence = store.get_evidence_docs([rid])
    assert len(evidence) == 2


def test_get_evidence_empty_for_unknown_relation(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "ev.db"))
    assert store.get_evidence_docs([9999]) == []


# ---------------------------------------------------------------------------
# 7. list_entities / list_relations
# ---------------------------------------------------------------------------


def test_list_entities_empty_initially(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "l.db"))
    assert store.list_entities() == []


def test_list_entities_returns_created_entities(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "l.db"))
    store.create_entity("Alice", "Person", "", None)
    store.create_entity("Acme", "Org", "", None)
    entities = store.list_entities()
    names = [e.name for e in entities]
    assert "Alice" in names
    assert "Acme" in names


def test_list_relations_empty_initially(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "l.db"))
    assert store.list_relations() == []


def test_list_relations_resolves_names(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "l.db"))
    doc = store.insert_document("text")
    e1 = store.create_entity("Alice", "Person", "", None)
    e2 = store.create_entity("Acme", "Org", "", None)
    store.upsert_relation(e1, e2, "works_at", doc)
    rels = store.list_relations()
    assert len(rels) == 1
    assert rels[0].source_name == "Alice"
    assert rels[0].target_name == "Acme"
    assert rels[0].predicate == "works_at"


# ---------------------------------------------------------------------------
# 8. find_or_create_entity (Phase 2 basic cascade)
# ---------------------------------------------------------------------------


def test_find_or_create_entity_creates_new(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "foc.db"))
    eid = store.find_or_create_entity("Alice", "Person", "desc", None)
    assert isinstance(eid, int)
    assert eid > 0


def test_find_or_create_entity_idempotent_by_alias(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "foc.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "desc", None)
    eid2 = store.find_or_create_entity("Alice", "Person", "longer description", None)
    assert eid1 == eid2


def test_find_or_create_entity_normalizes_name(tmp_path: Path) -> None:
    """'  ALICE  ' and 'alice' should resolve to the same entity."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "foc.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "", None)
    eid2 = store.find_or_create_entity("  ALICE  ", "Person", "", None)
    assert eid1 == eid2


def test_find_or_create_entity_updates_description_keep_longer(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "foc.db"))
    eid = store.find_or_create_entity("Alice", "Person", "short", None)
    store.find_or_create_entity("Alice", "Person", "much longer description here", None)
    row = store.get_entity(eid)
    assert row is not None
    assert row[3] == "much longer description here"


# ---------------------------------------------------------------------------
# 9. get_neighbors
# ---------------------------------------------------------------------------


def test_get_neighbors_returns_incident_edges(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "n.db"))
    doc = store.insert_document("text")
    e1 = store.create_entity("Alice", "Person", "", None)
    e2 = store.create_entity("Acme", "Org", "", None)
    e3 = store.create_entity("Bob", "Person", "", None)
    store.upsert_relation(e1, e2, "works_at", doc)
    store.upsert_relation(e3, e2, "founded", doc)
    edges = store.get_neighbors([e1])
    assert len(edges) == 1
    assert edges[0][1] == e1
    assert edges[0][2] == e2


def test_get_neighbors_empty_for_isolated_entity(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "n.db"))
    eid = store.create_entity("Lone", "Thing", "", None)
    assert store.get_neighbors([eid]) == []


def test_get_neighbors_empty_input(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "n.db"))
    assert store.get_neighbors([]) == []


# ---------------------------------------------------------------------------
# 10. MemoryAgent ingest_extracted (end-to-end Phase 2)
# ---------------------------------------------------------------------------


def test_agent_ingest_extracted_empty_graph(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import ExtractedGraph

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    result = agent.ingest_extracted("some text", ExtractedGraph(entities=[], relations=[]))
    assert result["doc_id"] > 0
    assert result["entities"] == 0
    assert result["relations"] == 0


def test_agent_ingest_extracted_with_entities(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(name="Alice", type="Person", description="engineer"),
            ExtractedEntity(name="Acme", type="Org", description="company"),
        ],
        relations=[
            ExtractedRelation(source="Alice", target="Acme", predicate="works_at"),
        ],
    )
    result = agent.ingest_extracted("Alice works at Acme.", graph)
    assert result["entities"] == 2
    assert result["relations"] == 1


def test_agent_ingest_extracted_persists_to_db(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import ExtractedEntity, ExtractedGraph

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    graph = ExtractedGraph(
        entities=[ExtractedEntity(name="Alice", type="Person")],
        relations=[],
    )
    agent.ingest_extracted("Alice is an engineer.", graph)
    entities = agent.list_entities()
    names = [e.name for e in entities]
    assert "Alice" in names


def test_agent_list_entities_returns_list(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    result = agent.list_entities()
    assert isinstance(result, list)


def test_agent_list_relations_returns_list(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    result = agent.list_relations()
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 11. CLI commands (Phase 2 functional)
# ---------------------------------------------------------------------------


def test_cli_ingest_writes_to_db(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    db = str(tmp_path / "cli.db")
    result = runner.invoke(app, ["ingest", "test text", "--db", db])
    assert result.exit_code == 0, result.output
    assert "Ingested" in result.output


def test_cli_list_entities_empty_db(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["list-entities", "--db", str(tmp_path / "cli.db")])
    assert result.exit_code == 0, result.output


def test_cli_list_relations_empty_db(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["list-relations", "--db", str(tmp_path / "cli.db")])
    assert result.exit_code == 0, result.output


def test_cli_ingest_file_works(tmp_path: Path) -> None:
    from membox.cli import app

    f = tmp_path / "data.txt"
    f.write_text("Alice works at Acme Corp.")
    runner = CliRunner()
    result = runner.invoke(app, ["ingest-file", str(f), "--db", str(tmp_path / "cli.db")])
    assert result.exit_code == 0, result.output
    assert "Ingested" in result.output


# ---------------------------------------------------------------------------
# 12. Thread safety: concurrent inserts produce distinct entity rows
# ---------------------------------------------------------------------------


def test_concurrent_find_or_create_no_duplicate(tmp_path: Path) -> None:
    """Two threads racing to find_or_create_entity("Alice") should yield the same eid."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "mt.db"))
    results: list[int] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            eid = store.find_or_create_entity("Alice", "Person", "desc", None)
            results.append(eid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 4
    assert len(set(results)) == 1, "All threads must get the same entity_id"
