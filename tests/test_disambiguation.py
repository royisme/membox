"""Phase 4 tests: entity disambiguation — alias, embedding, and concurrency."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ControlledEmbedder:
    """Test embedder that returns pre-configured vectors for known keys.

    Unknown keys return a zero vector (won't match any entity via cosine).
    """

    dim: int = 4

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._map = mapping

    def embed(self, text: str) -> list[float]:
        return self._map.get(text, [0.0] * self.dim)


# Unit vectors and near-neighbours (4-dimensional).
# Cosine("alice", "alicia") ≈ 0.98 — should merge (≥ 0.85).
# Cosine("alice", "bob")    = 0.0  — orthogonal, should NOT merge.
_ALICE = [1.0, 0.0, 0.0, 0.0]
_ALICIA = [0.98, 0.20, 0.0, 0.0]  # cos(Alice, Alicia) ≈ 0.979
_BOB = [0.0, 1.0, 0.0, 0.0]  # orthogonal to Alice


def _embedder(extra: dict[str, list[float]] | None = None) -> _ControlledEmbedder:
    base = {"Alice": _ALICE, "Alicia": _ALICIA, "Bob": _BOB}
    if extra:
        base.update(extra)
    return _ControlledEmbedder(base)


# ---------------------------------------------------------------------------
# Layer 1 — exact alias match (no embedder)
# ---------------------------------------------------------------------------


def test_exact_alias_dedup_same_name(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "desc", None)
    eid2 = store.find_or_create_entity("Alice", "Person", "desc", None)
    assert eid1 == eid2


def test_case_normalization_dedup(tmp_path: Path) -> None:
    """'ALICE' and 'alice' must resolve to the same entity."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "", None)
    eid2 = store.find_or_create_entity("ALICE", "Person", "", None)
    assert eid1 == eid2


def test_whitespace_normalization_dedup(tmp_path: Path) -> None:
    """'  Alice  ' must resolve to the same entity as 'Alice'."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid1 = store.find_or_create_entity("Alice Smith", "Person", "", None)
    eid2 = store.find_or_create_entity("  alice   smith  ", "Person", "", None)
    assert eid1 == eid2


def test_different_names_create_different_entities(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "", None)
    eid2 = store.find_or_create_entity("Bob", "Person", "", None)
    assert eid1 != eid2


def test_same_name_different_types_with_no_embedder(tmp_path: Path) -> None:
    """Without embedder, alias match is purely string; type is ignored."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid1 = store.find_or_create_entity("Python", "Language", "", None)
    eid2 = store.find_or_create_entity("Python", "Snake", "", None)
    # Same alias "python" → must resolve to the same entity (alias wins)
    assert eid1 == eid2


# ---------------------------------------------------------------------------
# Layer 2 — embedding cosine similarity
# ---------------------------------------------------------------------------


def test_embedding_synonym_dedup_above_threshold(tmp_path: Path) -> None:
    """'Alicia' should resolve to Alice's entity when cosine ≥ 0.85."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    emb = _embedder()
    eid_alice = store.find_or_create_entity("Alice", "Person", "engineer", emb)
    eid_alicia = store.find_or_create_entity("Alicia", "Person", "engineer", emb)
    assert eid_alice == eid_alicia


def test_embedding_no_merge_below_threshold(tmp_path: Path) -> None:
    """Bob (orthogonal to Alice) must NOT be merged with Alice."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    emb = _embedder()
    eid_alice = store.find_or_create_entity("Alice", "Person", "", emb)
    eid_bob = store.find_or_create_entity("Bob", "Person", "", emb)
    assert eid_alice != eid_bob


def test_embedding_no_merge_different_types(tmp_path: Path) -> None:
    """Even if cos ≥ 0.85, entities of different types should NOT merge."""
    from membox.store import KnowledgeStore

    # Give "Alicia" the same high-similarity vector but use a different type.
    # find_similar_entity restricts scan to type_, so type mismatch prevents merge.
    store = KnowledgeStore(str(tmp_path / "e.db"))
    emb = _embedder()
    eid_person = store.find_or_create_entity("Alice", "Person", "", emb)
    eid_bot = store.find_or_create_entity("Alicia", "Bot", "", emb)
    assert eid_person != eid_bot


def test_embedding_alias_registered_after_merge(tmp_path: Path) -> None:
    """After a cosine merge, the new alias must be queryable."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    emb = _embedder()
    eid_alice = store.find_or_create_entity("Alice", "Person", "", emb)
    store.find_or_create_entity("Alicia", "Person", "", emb)
    # "alicia" alias should now point to eid_alice
    assert store.find_entity_by_alias("alicia") == eid_alice


def test_fallback_to_create_when_no_embedder(tmp_path: Path) -> None:
    """With no embedder, unknown names always create new entities."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "e.db"))
    eid1 = store.find_or_create_entity("Alice", "Person", "", None)
    eid2 = store.find_or_create_entity("Alicia", "Person", "", None)
    # No embedder → can't detect similarity → two distinct entities
    assert eid1 != eid2


# ---------------------------------------------------------------------------
# Layer 3 — description update heuristic
# ---------------------------------------------------------------------------


def test_description_keeps_longer(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid = store.find_or_create_entity("Alice", "Person", "eng", None)
    store.find_or_create_entity("Alice", "Person", "senior software engineer", None)
    row = store.get_entity(eid)
    assert row is not None
    assert row[3] == "senior software engineer"


def test_description_does_not_overwrite_with_shorter(tmp_path: Path) -> None:
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "d.db"))
    eid = store.find_or_create_entity("Alice", "Person", "senior software engineer", None)
    store.find_or_create_entity("Alice", "Person", "eng", None)
    row = store.get_entity(eid)
    assert row is not None
    assert row[3] == "senior software engineer"


# ---------------------------------------------------------------------------
# Concurrency — 8 threads racing find_or_create_entity
# ---------------------------------------------------------------------------


def test_concurrent_8_threads_create_exactly_one_entity(tmp_path: Path) -> None:
    """8 threads racing to find_or_create_entity('Alice') must all get the same eid."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "mt.db"))
    results: list[int] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            eid = store.find_or_create_entity("Alice", "Person", "concurrent test", None)
            results.append(eid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 8  # all threads returned
    assert len(set(results)) == 1, f"Expected 1 unique entity_id, got: {set(results)}"
    # Confirm only one row in the DB
    count = store._conn().execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 1


def test_concurrent_distinct_names_each_get_own_entity(tmp_path: Path) -> None:
    """8 threads each creating a distinct entity should produce 8 rows."""
    from membox.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "mt2.db"))
    results: dict[str, int] = {}
    errors: list[Exception] = []
    lock = threading.Lock()

    names = [f"Person{i}" for i in range(8)]

    def worker(name: str) -> None:
        try:
            eid = store.find_or_create_entity(name, "Person", "", None)
            with lock:
                results[name] = eid
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 8  # all 8 names resolved
    assert len(set(results.values())) == 8  # all 8 got distinct entity_ids
