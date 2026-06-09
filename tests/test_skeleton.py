"""Phase 1 skeleton tests: verify import chains, CLI commands, Protocol stubs, and instantiation."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Import chain
# ---------------------------------------------------------------------------


def test_all_public_imports() -> None:
    from membox import (  # noqa: F401
        DummyEmbedder,
        DummyExtractor,
        Embedder,
        Entity,
        ExtractedEntity,
        ExtractedGraph,
        ExtractedRelation,
        HopResult,
        KnowledgeStore,
        LLMExtractor,
        MemoryAgent,
        Relation,
        Triple,
    )


# ---------------------------------------------------------------------------
# 2. DummyExtractor
# ---------------------------------------------------------------------------


def test_dummy_extractor_returns_empty_graph() -> None:
    from membox.extract import DummyExtractor
    from membox.schema import ExtractedGraph

    ext = DummyExtractor()
    result = ext.extract("some text about Alice and Bob")
    assert isinstance(result, ExtractedGraph)
    assert result.entities == []
    assert result.relations == []


def test_dummy_extractor_query_entities_returns_empty_list() -> None:
    from membox.extract import DummyExtractor

    ext = DummyExtractor()
    assert ext.extract_query_entities("what is Python?") == []


# ---------------------------------------------------------------------------
# 3. DummyEmbedder
# ---------------------------------------------------------------------------


def test_dummy_embedder_returns_zero_vector() -> None:
    from membox.embed import DummyEmbedder

    emb = DummyEmbedder()
    v = emb.embed("hello world")
    assert isinstance(v, list)
    assert len(v) == emb.dim
    assert all(x == 0.0 for x in v)


def test_dummy_embedder_dim_attribute() -> None:
    from membox.embed import DummyEmbedder

    emb = DummyEmbedder()
    assert isinstance(emb.dim, int)
    assert emb.dim > 0


# ---------------------------------------------------------------------------
# 4. normalize
# ---------------------------------------------------------------------------


def test_normalize_name_lowercases_and_collapses_whitespace() -> None:
    from membox.normalize import normalize_name

    assert normalize_name("  Hello   World  ") == "hello world"
    assert normalize_name("Python") == "python"
    assert normalize_name("  UPPER  ") == "upper"


def test_normalize_predicate_lowercases() -> None:
    from membox.normalize import normalize_predicate

    assert normalize_predicate("USES") == "uses"
    assert normalize_predicate("  Develops  ") == "develops"
    assert normalize_predicate("RELATED_TO") == "related_to"


# ---------------------------------------------------------------------------
# 5. KnowledgeStore instantiation
# ---------------------------------------------------------------------------


def test_knowledge_store_instantiates(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "test.db"))
    assert store.db_path == str(tmp_path / "test.db")


def test_knowledge_store_conn_returns_sqlite_connection(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "test.db"))
    conn = store._conn()
    assert isinstance(conn, sqlite3.Connection)


def test_knowledge_store_conn_is_per_thread_cached(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "test.db"))
    conn1 = store._conn()
    conn2 = store._conn()
    assert conn1 is conn2


# ---------------------------------------------------------------------------
# 6. KnowledgeStore stubs raise NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,args",
    [
        ("insert_document", ("text",)),
        ("find_entity_by_alias", ("name",)),
        ("find_similar_entity", ([0.1, 0.2, 0.3, 0.4], "Type")),
        ("create_entity", ("name", "Type", "desc", None)),
        ("add_alias", ("alias", 1)),
        ("update_entity_description", (1, "new description")),
        ("find_or_create_entity", ("name", "Type", "desc", None)),
        ("upsert_relation", (1, 2, "uses", 1)),
        ("get_entity", (1,)),
        ("get_neighbors", ([1, 2],)),
        ("get_evidence_docs", ([1, 2],)),
        ("bfs_query", ([1], 2)),
        ("list_entities", ()),
        ("list_relations", ()),
    ],
)
def test_store_stubs_raise_not_implemented(
    tmp_path: Path, method: str, args: tuple[object, ...]
) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "test.db"))
    with pytest.raises(NotImplementedError):
        getattr(store, method)(*args)


# ---------------------------------------------------------------------------
# 7. MemoryAgent instantiation
# ---------------------------------------------------------------------------


def test_memory_agent_instantiates(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), embedder=None, db_path=str(tmp_path / "a.db"))
    assert agent.store.db_path == str(tmp_path / "a.db")


def test_memory_agent_store_attribute_is_knowledge_store(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.store import KnowledgeStore

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    assert isinstance(agent.store, KnowledgeStore)


# ---------------------------------------------------------------------------
# 8. MemoryAgent.query and to_prompt_context work with empty seeds
# ---------------------------------------------------------------------------


def test_query_with_dummy_extractor_returns_placeholder(tmp_path: Path) -> None:
    """DummyExtractor returns [] seeds → retrieve returns empty HopResult → placeholder."""
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "q.db"))
    result = agent.query("any question about anything")
    assert "没有找到" in result


def test_to_prompt_context_empty_returns_placeholder(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import HopResult

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "c.db"))
    ctx = agent.to_prompt_context(HopResult())
    assert "没有找到" in ctx


def test_to_prompt_context_with_data(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import HopResult

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "c2.db"))
    hop = HopResult(
        triplets=[("Alice", "works_at", "Acme")],
        documents=["Alice joined Acme in 2020."],
        seed_names=["Alice"],
        visited_entities=["Alice", "Acme"],
    )
    ctx = agent.to_prompt_context(hop)
    assert "[Alice]" in ctx
    assert "[Acme]" in ctx
    assert "works_at" in ctx
    assert "Alice joined Acme" in ctx


# ---------------------------------------------------------------------------
# 9. MemoryAgent stub methods raise NotImplementedError
# ---------------------------------------------------------------------------


def test_agent_ingest_raises_not_implemented(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    with pytest.raises(NotImplementedError):
        agent.ingest("some text about technology")


def test_agent_ingest_extracted_raises_not_implemented(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor
    from membox.schema import ExtractedGraph

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    with pytest.raises(NotImplementedError):
        agent.ingest_extracted("text", ExtractedGraph(entities=[], relations=[]))


def test_agent_list_entities_raises_not_implemented(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    with pytest.raises(NotImplementedError):
        agent.list_entities()


def test_agent_list_relations_raises_not_implemented(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    with pytest.raises(NotImplementedError):
        agent.list_relations()


# ---------------------------------------------------------------------------
# 10. CLI skeleton
# ---------------------------------------------------------------------------


def test_cli_help_shows_all_commands() -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["ingest", "ingest-file", "query", "list-entities", "list-relations", "version"]:
        assert cmd in result.output


def test_cli_version_command() -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "membox" in result.output
    assert "0.1.0" in result.output


def test_cli_query_with_empty_seeds_returns_placeholder(tmp_path: Path) -> None:
    """query with DummyExtractor never touches the store → works even in Phase 1."""
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["query", "any question", "--db", str(tmp_path / "q.db")])
    assert result.exit_code == 0
    assert "没有找到" in result.output


def test_cli_ingest_raises_before_phase2(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "test text", "--db", str(tmp_path / "i.db")])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)


def test_cli_list_entities_raises_before_phase2(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["list-entities", "--db", str(tmp_path / "e.db")])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)


def test_cli_list_relations_raises_before_phase2(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["list-relations", "--db", str(tmp_path / "e.db")])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)


def test_cli_ingest_file_not_found(tmp_path: Path) -> None:
    from membox.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["ingest-file", str(tmp_path / "nope.txt")])
    assert result.exit_code == 1


def test_cli_ingest_file_raises_before_phase2(tmp_path: Path) -> None:
    from membox.cli import app

    f = tmp_path / "data.txt"
    f.write_text("Alice works at Acme.")
    runner = CliRunner()
    result = runner.invoke(app, ["ingest-file", str(f), "--db", str(tmp_path / "i.db")])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)


def test_store_tx_context_manager_commits(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "tx.db"))
    with store._tx():
        pass  # auto-commits


def test_store_tx_context_manager_rollbacks_on_error(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "tx.db"))
    msg = "forced rollback"
    with pytest.raises(ValueError), store._tx():
        raise ValueError(msg)


def test_agent_retrieve_with_seeds_raises_not_implemented(tmp_path: Path) -> None:
    from membox.agent import MemoryAgent
    from membox.extract import DummyExtractor

    agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "a.db"))
    with pytest.raises(NotImplementedError):
        agent.retrieve(["Alice", "Acme"], max_hops=2)
