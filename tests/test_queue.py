"""Phase 7.5 M6 asynchronous-ingestion-queue tests.

Covers:
- Migration 0004: ingest_queue table on fresh and v3 databases.
- QueueOps: enqueue, atomic claim, done/failed transitions, retry ceiling,
  per-status counts, recent failures.
- Worker lease: acquire/refresh/release, expiry takeover, single-worker
  guarantee, crash recovery (stale processing rows reset to pending).
- drain_queue: drains to zero and exits (no daemon), per-item failure
  isolation, lease held by another worker → no-op.
- MemoryAgent.enqueue / enqueue_file: fast path writes only the queue row;
  enqueue latency < 100 ms at the API layer.
- Query footer: pending-ingests staleness note.
- CLI: `membox process` drains, `membox queue` prints counts and failures.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from membox.cli import app
from membox.core.agent import MemoryAgent
from membox.core.store import KnowledgeStore
from membox.core.worker import drain_queue
from membox.model.schema import ExtractedEntity, ExtractedGraph, ExtractedRelation
from membox.services.extraction import DummyExtractor

runner = CliRunner()


class _FakeExtractor:
    """Extractor returning one fixed triple per chunk; records call count."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str) -> ExtractedGraph:
        self.calls += 1
        return ExtractedGraph(
            entities=[
                ExtractedEntity(name="membox", type="Project", description=""),
                ExtractedEntity(name="SQLite", type="Technology", description=""),
            ],
            relations=[
                ExtractedRelation(source="membox", predicate="uses", target="SQLite"),
            ],
        )

    def extract_query_entities(self, query: str) -> list[str]:
        return ["membox"]


class _FailingExtractor:
    """Extractor that always raises, to exercise the failed path."""

    def extract(self, text: str) -> ExtractedGraph:
        msg = "extraction exploded"
        raise RuntimeError(msg)

    def extract_query_entities(self, query: str) -> list[str]:
        return []


def _stale_lease(store: KnowledgeStore, age_seconds: float = 120.0) -> None:
    """Write an expired lease owned by a fictitious dead worker."""
    heartbeat = (datetime.now(tz=UTC) - timedelta(seconds=age_seconds)).isoformat()
    lease = json.dumps({"pid": 999999, "hostname": "dead-host", "heartbeat": heartbeat})
    conn = store._conn()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('worker_lease', ?);", (lease,))


def _live_foreign_lease(store: KnowledgeStore) -> None:
    """Write a live lease owned by another (fictitious) process."""
    heartbeat = datetime.now(tz=UTC).isoformat()
    lease = json.dumps({"pid": 999999, "hostname": "other-host", "heartbeat": heartbeat})
    conn = store._conn()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('worker_lease', ?);", (lease,))


# ──────────────────────────────────────────────────────────────────────────────
# 1. Migration 0004
# ──────────────────────────────────────────────────────────────────────────────


class TestMigration0004:
    """Migration 0004 creates the ingest_queue table."""

    def test_fresh_db_has_ingest_queue_table(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "fresh.db"))
        tables = {
            row[0]
            for row in store._conn()
            .execute("SELECT name FROM sqlite_master WHERE type='table';")
            .fetchall()
        }
        assert "ingest_queue" in tables

    def test_queue_columns(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "fresh.db"))
        cols = {
            row[1] for row in store._conn().execute("PRAGMA table_info(ingest_queue);").fetchall()
        }
        assert {
            "id",
            "content",
            "project",
            "source_path",
            "doc_date",
            "status",
            "retries",
            "error",
            "enqueued_at",
            "started_at",
            "finished_at",
        }.issubset(cols)


# ──────────────────────────────────────────────────────────────────────────────
# 2. QueueOps
# ──────────────────────────────────────────────────────────────────────────────


class TestQueueOps:
    """Enqueue / claim / complete state machine."""

    def test_enqueue_returns_id_and_pending_status(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        qid = store.enqueue_ingest("hello", project="p", source_path="/x.md", doc_date="2026-06-10")
        assert qid == 1
        assert store.queue_counts()["pending"] == 1

    def test_claim_moves_pending_to_processing(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        store.enqueue_ingest("first")
        store.enqueue_ingest("second")
        item = store.claim_next_pending()
        assert item is not None
        assert item["content"] == "first"  # FIFO by id
        counts = store.queue_counts()
        assert counts["processing"] == 1
        assert counts["pending"] == 1

    def test_claim_returns_none_when_empty(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        assert store.claim_next_pending() is None

    def test_mark_done_and_failed(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        a = store.enqueue_ingest("a")
        b = store.enqueue_ingest("b")
        store.claim_next_pending()
        store.mark_done(a)
        store.claim_next_pending()
        store.mark_failed(b, "boom")
        counts = store.queue_counts()
        assert counts == {"pending": 0, "processing": 0, "done": 1, "failed": 1}
        failures = store.recent_failures()
        assert failures[0]["id"] == b
        assert failures[0]["error"] == "boom"
        assert failures[0]["retries"] == 1

    def test_retry_failed_respects_max_retries(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        qid = store.enqueue_ingest("a")
        for _ in range(3):
            store.claim_next_pending()
            store.mark_failed(qid, "boom")
            store.retry_failed()
        # retries == 3 now: permanently failed, no further reset.
        store.claim_next_pending()
        store.mark_failed(qid, "boom")
        assert store.retry_failed() == 0
        assert store.queue_counts()["failed"] == 1

    def test_pending_ingest_count_includes_processing(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        store.enqueue_ingest("a")
        store.enqueue_ingest("b")
        store.claim_next_pending()
        assert store.pending_ingest_count() == 2


# ──────────────────────────────────────────────────────────────────────────────
# 3. Worker lease
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkerLease:
    """Single-worker guarantee and crash recovery."""

    def test_acquire_when_no_lease(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        assert store.acquire_worker_lease() is True
        assert store.worker_is_alive() is True

    def test_acquire_blocked_by_live_foreign_lease(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        _live_foreign_lease(store)
        assert store.acquire_worker_lease() is False

    def test_acquire_takes_over_expired_lease(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        _stale_lease(store)
        assert store.acquire_worker_lease() is True

    def test_takeover_resets_stale_processing_rows(self, tmp_path: Path) -> None:
        """Crash recovery: a dead worker's processing rows return to pending."""
        store = KnowledgeStore(str(tmp_path / "q.db"))
        store.enqueue_ingest("orphaned")
        store.claim_next_pending()  # row now 'processing', as if a worker died mid-item
        _stale_lease(store)
        assert store.acquire_worker_lease() is True
        assert store.queue_counts()["pending"] == 1
        assert store.queue_counts()["processing"] == 0

    def test_release_deletes_own_lease(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        store.acquire_worker_lease()
        store.release_worker_lease()
        assert store.worker_is_alive() is False

    def test_release_keeps_foreign_lease(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        _live_foreign_lease(store)
        store.release_worker_lease()
        assert store.worker_is_alive() is True

    def test_malformed_lease_treated_as_expired(self, tmp_path: Path) -> None:
        store = KnowledgeStore(str(tmp_path / "q.db"))
        conn = store._conn()
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('worker_lease', 'not json');")
        assert store.worker_is_alive() is False
        assert store.acquire_worker_lease() is True


# ──────────────────────────────────────────────────────────────────────────────
# 4. drain_queue (the worker body)
# ──────────────────────────────────────────────────────────────────────────────


class TestDrainQueue:
    """The drain loop materializes queue rows and exits — no daemon."""

    def test_drains_to_zero_and_returns(self, tmp_path: Path) -> None:
        """The acceptance test for 'no daemon': drain_queue returns when empty."""
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("## A\nmembox uses SQLite.", source_path="/a.md")
        agent.enqueue("plain text doc")
        stats = drain_queue(agent)
        assert stats["done"] == 2
        assert stats["failed"] == 0
        assert agent.store.pending_ingest_count() == 0
        # Materialized: documents + graph rows exist now.
        assert len(agent.list_relations()) == 1

    def test_lease_released_after_drain(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("doc")
        drain_queue(agent)
        assert agent.store.worker_is_alive() is False

    def test_failed_item_isolated_and_recorded(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FailingExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("will fail")
        stats = drain_queue(agent)
        assert stats == {"done": 0, "failed": 1, "retried": 0}
        failures = agent.store.recent_failures()
        assert "extraction exploded" in str(failures[0]["error"])

    def test_retry_failed_flag_resets_then_drains(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FailingExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("will fail")
        drain_queue(agent)
        ok_agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        stats = drain_queue(ok_agent, retry_failed=True)
        assert stats["retried"] == 1
        assert stats["done"] == 1
        assert ok_agent.store.queue_counts()["failed"] == 0

    def test_noop_when_foreign_lease_live(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("doc")
        _live_foreign_lease(agent.store)
        stats = drain_queue(agent)
        assert stats == {"done": 0, "failed": 0, "retried": 0}
        assert agent.store.pending_ingest_count() == 1

    def test_crash_recovery_end_to_end(self, tmp_path: Path) -> None:
        """A killed worker's processing row is completed by the next worker."""
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("interrupted doc")
        agent.store.claim_next_pending()  # simulate worker death mid-item
        _stale_lease(agent.store)
        stats = drain_queue(agent)
        assert stats["done"] == 1
        assert agent.store.pending_ingest_count() == 0


# ──────────────────────────────────────────────────────────────────────────────
# 5. Agent enqueue API (fast path)
# ──────────────────────────────────────────────────────────────────────────────


class TestAgentEnqueue:
    """MemoryAgent.enqueue / enqueue_file write only a queue row, fast."""

    def test_enqueue_writes_queue_row_only(self, tmp_path: Path) -> None:
        extractor = _FakeExtractor()
        agent = MemoryAgent(extractor=extractor, db_path=str(tmp_path / "q.db"))
        qid = agent.enqueue("some text", project="p")
        assert qid == 1
        assert extractor.calls == 0  # no LLM call on the enqueue path
        conn = agent.store._conn()
        assert conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0] == 0

    def test_enqueue_file_captures_metadata(self, tmp_path: Path) -> None:
        md = tmp_path / "doc.md"
        md.write_text("## S\nBody.", encoding="utf-8")
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue_file(md)
        row = (
            agent.store._conn()
            .execute("SELECT content, project, source_path, doc_date FROM ingest_queue;")
            .fetchone()
        )
        assert row[0] == "## S\nBody."
        assert row[1]  # project inferred (parent dir fallback)
        assert row[2] == str(md.resolve())
        assert row[3]  # mtime date

    def test_enqueue_latency_under_100ms(self, tmp_path: Path) -> None:
        """Acceptance: write acceptance is milliseconds at the API layer."""
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "q.db"))
        payload = "x" * 5_000  # ~5 KB, per the acceptance criterion
        agent.enqueue("warmup")  # exclude first-call connection setup
        start = time.perf_counter()
        agent.enqueue(payload, project="p", source_path="/big.md")
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 100

    def test_drained_queue_matches_sync_ingest_rows(self, tmp_path: Path) -> None:
        """Async path materializes the same document metadata as sync ingest."""
        md = tmp_path / "doc.md"
        md.write_text("## Alpha\nmembox uses SQLite.", encoding="utf-8")
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue_file(md)
        drain_queue(agent)
        row = (
            agent.store._conn()
            .execute("SELECT section, source_path, version FROM documents;")
            .fetchone()
        )
        assert row[0] == "Alpha"
        assert row[1] == str(md.resolve())
        assert row[2] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 6. Query staleness footer
# ──────────────────────────────────────────────────────────────────────────────


class TestQueryPendingNote:
    """membox query surfaces pending ingests — silent staleness is forbidden."""

    def test_footer_notes_pending_ingests(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.ingest("membox uses SQLite.")  # sync: graph has content
        agent.enqueue("not yet materialized")
        output = agent.query("what does membox use?")
        assert "1 ingest(s) pending" in output
        assert "may be incomplete" in output

    def test_no_note_when_queue_empty(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=_FakeExtractor(), db_path=str(tmp_path / "q.db"))
        agent.ingest("membox uses SQLite.")
        output = agent.query("what does membox use?")
        assert "pending" not in output

    def test_note_present_even_with_no_seeds(self, tmp_path: Path) -> None:
        agent = MemoryAgent(extractor=DummyExtractor(), db_path=str(tmp_path / "q.db"))
        agent.enqueue("queued doc")
        output = agent.query("anything")
        assert "1 ingest(s) pending" in output


# ──────────────────────────────────────────────────────────────────────────────
# 7. CLI process / queue commands
# ──────────────────────────────────────────────────────────────────────────────


class TestCliQueueCommands:
    """`membox process` and `membox queue`."""

    def test_process_drains_queue(self, tmp_path: Path) -> None:
        db = str(tmp_path / "q.db")
        runner.invoke(app, ["ingest", "doc one", "--db", db, "--no-spawn"])
        runner.invoke(app, ["ingest", "doc two", "--db", db, "--no-spawn"])
        result = runner.invoke(app, ["process", "--db", db, "--no-llm"])
        assert result.exit_code == 0
        assert "2 done" in result.stdout
        conn = sqlite3.connect(db)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM ingest_queue WHERE status='pending';"
            ).fetchone()[0]
        finally:
            conn.close()
        assert pending == 0

    def test_queue_shows_counts(self, tmp_path: Path) -> None:
        db = str(tmp_path / "q.db")
        runner.invoke(app, ["ingest", "doc", "--db", db, "--no-spawn"])
        result = runner.invoke(app, ["queue", "--db", db])
        assert result.exit_code == 0
        assert "pending" in result.stdout
        assert "1" in result.stdout

    def test_queue_shows_recent_failures(self, tmp_path: Path) -> None:
        db = str(tmp_path / "q.db")
        store = KnowledgeStore(db)
        qid = store.enqueue_ingest("bad doc", source_path="/bad.md")
        store.claim_next_pending()
        store.mark_failed(qid, "extraction exploded")
        store.close()
        result = runner.invoke(app, ["queue", "--db", db])
        assert result.exit_code == 0
        assert "extraction exploded" in result.stdout

    def test_query_cli_includes_pending_note(self, tmp_path: Path) -> None:
        db = str(tmp_path / "q.db")
        runner.invoke(app, ["ingest", "doc", "--db", db, "--no-spawn"])
        result = runner.invoke(app, ["query", "anything", "--db", db, "--no-llm"])
        assert result.exit_code == 0
        assert "pending" in result.stdout


# ──────────────────────────────────────────────────────────────────────────────
# 8. spawn_worker end-to-end (real detached subprocess)
# ──────────────────────────────────────────────────────────────────────────────


class TestSpawnWorker:
    """spawn_worker launches a transient subprocess that drains and exits."""

    def test_spawn_skipped_when_lease_alive(self, tmp_path: Path) -> None:
        from membox.core.worker import spawn_worker

        db = str(tmp_path / "q.db")
        store = KnowledgeStore(db)
        _live_foreign_lease(store)
        store.close()
        assert spawn_worker(db) is False

    @pytest.mark.timeout(35)
    def test_spawned_worker_drains_queue_and_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end no-daemon proof: real subprocess drains then terminates."""
        from membox.core.worker import spawn_worker

        # The spawned subprocess inherits this env: force the no-op extractor
        # so the test never makes a network call.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        db = str(tmp_path / "q.db")
        store = KnowledgeStore(db)
        store.enqueue_ingest("Alice works at Acme.", source_path="/note.txt")
        store.close()

        assert spawn_worker(db) is True

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            conn = sqlite3.connect(db)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM ingest_queue WHERE status IN ('pending', 'processing');"
                ).fetchone()[0]
                lease = conn.execute(
                    "SELECT COUNT(*) FROM meta WHERE key='worker_lease';"
                ).fetchone()[0]
            finally:
                conn.close()
            if remaining == 0 and lease == 0:
                break
            time.sleep(0.2)
        else:
            log = Path(f"{db}.worker.log")
            detail = log.read_text(encoding="utf-8") if log.exists() else "(no log)"
            msg = f"worker did not drain queue within 30s; log:\n{detail}"
            raise AssertionError(msg)

        # Lease deleted on exit (released) — the worker is not lingering.
        assert Path(f"{db}.worker.log").exists()
