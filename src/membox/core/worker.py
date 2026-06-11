"""Ingestion queue worker — drain loop, lease management, crash recovery (M6).

The worker is a transient process, not a daemon: it acquires the
``worker_lease`` (taking over expired leases and resetting stale
``processing`` rows — crash recovery), drains the queue one item at a time
through the existing chunk → extract → embed → store pipeline, refreshes the
lease heartbeat after each item, and exits when no pending rows remain.

:func:`spawn_worker` launches ``membox process`` as a detached subprocess
(``start_new_session=True``) with stdout/stderr appended to ``<db>.worker.log``
so the enqueue path returns in milliseconds while materialization proceeds in
the background.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from membox.core.store.queue import DEFAULT_MAX_RETRIES

if TYPE_CHECKING:
    from membox.core.agent import MemoryAgent


def drain_queue(
    agent: MemoryAgent,
    *,
    retry_failed: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, int]:
    """Drain the ingest queue and return per-outcome counts.

    Acquires the worker lease first; returns immediately with zero counts
    when another live worker already holds it (single-worker guarantee).
    Each claimed item runs the full synchronous pipeline via
    :meth:`~membox.core.agent.MemoryAgent.ingest_content`.  An item whose
    chunks *all* fail extraction is marked ``failed``; partial success counts
    as ``done`` (per-chunk failure isolation already recorded the errors on
    the chunk level, consistent with the sync path).

    Args:
        agent: Configured MemoryAgent whose store owns the queue.
        retry_failed: Reset retryable failed rows to pending before draining.
        max_retries: Retry ceiling passed to the reset.

    Returns:
        Dict with keys ``done``, ``failed``, and ``retried`` (rows reset by
        *retry_failed*).  All zero when the lease was held by another worker.
    """
    store = agent.store
    if not store.acquire_worker_lease():
        return {"done": 0, "failed": 0, "retried": 0}

    retried = store.retry_failed(max_retries) if retry_failed else 0
    done = 0
    failed = 0
    try:
        while True:
            item = store.claim_next_pending()
            if item is None:
                break
            queue_id = item["id"]
            try:
                results = agent.ingest_content(
                    item["content"],
                    source=item["source_path"] or "",
                    project=item["project"],
                    source_path=item["source_path"],
                    doc_date=item["doc_date"],
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # isolate per-item failures; queue must drain
                store.mark_failed(queue_id, str(exc))
                failed += 1
            else:
                chunk_errors = [r["error"] for r in results if "error" in r]
                if results and len(chunk_errors) == len(results):
                    store.mark_failed(queue_id, str(chunk_errors[0]))
                    failed += 1
                else:
                    store.mark_done(queue_id)
                    done += 1
            store.refresh_worker_lease()
    finally:
        store.release_worker_lease()

    return {"done": done, "failed": failed, "retried": retried}


def spawn_worker(db_path: str) -> bool:
    """Spawn a detached ``membox process`` subprocess if no worker is alive.

    The subprocess is started with ``start_new_session=True`` so it outlives
    the enqueueing process; its output is appended to ``<db>.worker.log``.
    Liveness is checked via the worker lease, so repeated enqueues do not
    pile up workers.

    Args:
        db_path: Path to the SQLite database the worker should drain.

    Returns:
        True when a worker subprocess was spawned; False when a live worker
        lease already exists.
    """
    from membox.core.store import KnowledgeStore

    store = KnowledgeStore(db_path)
    try:
        if store.worker_is_alive():
            return False
    finally:
        store.close()

    log_path = f"{db_path}.worker.log"
    with open(log_path, "a", encoding="utf-8") as log:
        subprocess.Popen(  # noqa: S603 — argv is fixed, no shell
            [sys.executable, "-m", "membox.cli", "process", "--db", db_path],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return True
