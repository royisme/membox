"""Phase 6 tests: concurrency hardening — per-thread connections, WAL, RLock, multi-process."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from membox.model.schema import Entity


# ---------------------------------------------------------------------------
# 1. Per-thread connection isolation
# ---------------------------------------------------------------------------


def test_per_thread_connections_are_distinct(tmp_path: Path) -> None:
    """Each thread must get its own SQLite connection object."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "pt.db"))
    conns: list[sqlite3.Connection] = []
    lock = threading.Lock()

    def grab() -> None:
        try:
            c = store._conn()
            with lock:
                conns.append(c)
        finally:
            store.close()

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conns.append(store._conn())  # main thread
    # All 4 worker connections plus 1 main must be distinct objects
    assert len({id(c) for c in conns}) == 5


def test_same_thread_reuses_connection(tmp_path: Path) -> None:
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "pt.db"))
    c1 = store._conn()
    c2 = store._conn()
    assert c1 is c2


# ---------------------------------------------------------------------------
# 2. WAL mode — concurrent reader and writer
# ---------------------------------------------------------------------------


def test_wal_allows_read_during_write(tmp_path: Path) -> None:
    """WAL mode: a reader can list_entities while a writer holds a transaction open."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "wal.db"))
    store.create_entity("Seed", "Thing", "", None)

    writer_started = threading.Event()
    reader_done = threading.Event()
    read_result: list[Entity] = []
    errors: list[Exception] = []

    def writer() -> None:
        try:
            # Open a long write transaction
            with store._tx() as conn:
                conn.execute(
                    "INSERT INTO entities(canonical_name, type, description) "
                    "VALUES ('Writer', 'Thing', '')"
                )
                writer_started.set()
                # Hold the transaction open until the reader has finished
                reader_done.wait(timeout=5.0)
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    def reader() -> None:
        writer_started.wait(timeout=5.0)
        try:
            # WAL: this must not block even though a write transaction is open
            entities = store.list_entities()
            read_result.extend(entities)
        except Exception as exc:
            errors.append(exc)
        finally:
            reader_done.set()
            store.close()

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    wt.join(timeout=10.0)
    rt.join(timeout=10.0)

    assert not errors, f"Errors: {errors}"
    # Reader sees at least the Seed entity committed before the transaction
    names = [e.name for e in read_result]
    assert "Seed" in names


# ---------------------------------------------------------------------------
# 3. 5 threads x 10 writes — no errors, exact counts
# ---------------------------------------------------------------------------


def test_concurrent_5x10_ingest_no_errors(tmp_path: Path) -> None:
    """5 threads each ingest 10 distinct documents → 50 documents, no errors."""
    from membox.core.agent import MemoryAgent
    from membox.core.store import KnowledgeStore
    from membox.model.schema import ExtractedEntity, ExtractedGraph
    from membox.services.extraction import DummyExtractor

    db = str(tmp_path / "ingest.db")
    KnowledgeStore(db).close()
    errors: list[Exception] = []
    barrier = threading.Barrier(5)

    def worker(thread_id: int) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=db)
        try:
            barrier.wait(timeout=5.0)  # all threads start simultaneously
            for i in range(10):
                graph = ExtractedGraph(
                    entities=[ExtractedEntity(name=f"Entity-{thread_id}-{i}", type="Thing")],
                    relations=[],
                )
                agent.ingest_extracted(f"doc-{thread_id}-{i}", graph)
        except Exception as exc:
            errors.append(exc)
        finally:
            agent.store.close()

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    # Verify exact document count
    store = KnowledgeStore(db)
    doc_count = store._conn().execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert doc_count == 50
    # All 5x10 distinct entity names must have persisted as entity rows
    names = {e.name for e in store.list_entities()}
    expected = {f"Entity-{tid}-{i}" for tid in range(5) for i in range(10)}
    assert names == expected


def test_concurrent_5x10_same_entity_dedup(tmp_path: Path) -> None:
    """5 threads each ingesting the SAME entity name 10 times → exactly 1 entity row."""
    from membox.core.agent import MemoryAgent
    from membox.core.store import KnowledgeStore
    from membox.model.schema import ExtractedEntity, ExtractedGraph
    from membox.services.extraction import DummyExtractor

    db = str(tmp_path / "dedup.db")
    KnowledgeStore(db).close()
    errors: list[Exception] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=db)
        try:
            barrier.wait(timeout=5.0)
            for _ in range(10):
                graph = ExtractedGraph(
                    entities=[ExtractedEntity(name="SharedEntity", type="Thing")],
                    relations=[],
                )
                agent.ingest_extracted("text", graph)
        except Exception as exc:
            errors.append(exc)
        finally:
            agent.store.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"

    store = KnowledgeStore(db)
    entity_count = store._conn().execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert entity_count == 1


# ---------------------------------------------------------------------------
# 4. RLock prevents duplicate entity creation under heavy contention
# ---------------------------------------------------------------------------


def test_rlock_prevents_duplicate_entity_heavy_contention(tmp_path: Path) -> None:
    """20 threads racing find_or_create_entity on the same name → exactly 1 entity row."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "rlock.db"))
    results: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(20)

    def worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            eid = store.find_or_create_entity("HotEntity", "Thing", "contested", None)
            results.append(eid)
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert len(results) == 20
    assert len(set(results)) == 1, f"Duplicate entities created: {set(results)}"
    count = store._conn().execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 5. Concurrent relations from different threads
# ---------------------------------------------------------------------------


def test_concurrent_relation_inserts_dedup_correctly(tmp_path: Path) -> None:
    """10 threads each trying to insert the same relation → exactly 1 relation row."""
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "rel.db"))
    doc_id = store.insert_document("shared doc")
    e1 = store.find_or_create_entity("A", "Thing", "", None)
    e2 = store.find_or_create_entity("B", "Thing", "", None)
    results: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            rid = store.upsert_relation(e1, e2, "links", doc_id)
            results.append(rid)
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert len(set(results)) == 1, f"Multiple relation rows: {set(results)}"
    count = store._conn().execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 6. Multi-process safety: find_or_create_entity across real processes
# ---------------------------------------------------------------------------

_MP_NAME_COUNT = 50
_MP_PROC_COUNT = 4

# Fresh interpreters via subprocess: multiprocessing fork() deadlocks on macOS
# after this suite has spawned threads, and spawn cannot pickle test-module
# functions under pytest's importlib mode.
_MP_WORKER_SCRIPT = """
import json, sys, time
from pathlib import Path
from membox.core.store import KnowledgeStore

db, go_file, name_count = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
store = KnowledgeStore(db)
deadline = time.monotonic() + 30.0
while not go_file.exists():          # start gate so workers race, not stagger
    if time.monotonic() > deadline:
        raise SystemExit("start gate never opened")
    time.sleep(0.001)
ids = {}
for i in range(name_count):
    name = f"Contested-{i}"
    ids[name] = store.find_or_create_entity(name, "Thing", "raced", None)
store.close()
print(json.dumps(ids))
"""


@pytest.mark.timeout(65)
def test_multiprocess_find_or_create_same_names_no_duplicates(tmp_path: Path) -> None:
    """4 processes racing find_or_create_entity on 50 shared names → identical ids, 50 rows.

    The in-process RLock cannot serialize separate processes; this exercises
    the IntegrityError re-resolution path in find_or_create_entity.
    """
    from membox.core.store import KnowledgeStore

    db = str(tmp_path / "mp.db")
    KnowledgeStore(db).close()  # initialize schema before workers start
    go_file = tmp_path / "go"

    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _MP_WORKER_SCRIPT, db, str(go_file), str(_MP_NAME_COUNT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(_MP_PROC_COUNT)
    ]
    go_file.touch()  # open the start gate

    mappings: list[dict[str, int]] = []
    errors: list[str] = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=60.0)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            errors.append(f"worker timed out: {err}")
            continue
        if p.returncode != 0:
            errors.append(f"worker exited {p.returncode}: {err}")
        else:
            mappings.append(json.loads(out))

    assert not errors, f"Worker errors: {errors}"
    assert len(mappings) == _MP_PROC_COUNT
    # Every process must agree on the id of every contested name
    first = mappings[0]
    for other in mappings[1:]:
        assert other == first, f"Processes disagree on entity ids: {first} != {other}"

    store = KnowledgeStore(db)
    count = store._conn().execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == _MP_NAME_COUNT
    store.close()
