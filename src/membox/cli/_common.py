"""Shared helpers for membox CLI commands: console, agent factory, notices."""

from __future__ import annotations

import typer
from rich.console import Console

from membox.config import MemboxConfig
from membox.core.agent import MemoryAgent
from membox.services.extraction import DummyExtractor, create_default_extractor

console = Console()

NO_LLM_NOTICE = (
    "no extractor configured — entities not extracted; configure an LLM or use "
    "the agent-as-provider flow (membox extract-prompt → ingest-graph)"
)


def make_agent(
    db: str,
    no_llm: bool = False,
    concurrency: int | None = None,
    *,
    warn_no_extractor: bool = True,
) -> MemoryAgent:
    """Create a MemoryAgent using the best available extraction backend.

    Whenever the constructed extractor is a :class:`DummyExtractor` (no LLM
    available, ``--no-llm`` requested, or the ``openai`` package missing),
    a warning is printed to stderr pointing at the agent-as-provider flow
    so the silent zero-entity ingest footgun never recurs.  The warning is
    emitted in both the sync and async (enqueue) code paths.

    Args:
        db: Path to the SQLite database file.
        no_llm: Force the no-op Dummy backend even if OpenAI is available.
        concurrency: Optional override for per-chunk extraction+embedding
            parallelism.  When None, falls back to the value from
            ``MemboxConfig().ingest.concurrency`` (which itself defaults to
            ``MEMBOX_INGEST_CONCURRENCY`` or 1).  Values below 1 are clamped
            to 1 by ``MemoryAgent``.
        warn_no_extractor: Emit the no-extractor notice when the backend is a
            Dummy.  ``ingest-graph`` sets this False: it bypasses extraction by
            design (the agent already extracted), so the footgun warning would
            be a confusing self-reference, not a missing-LLM signal.

    Returns:
        Configured MemoryAgent.
    """
    config = MemboxConfig()
    extractor, embedder = create_default_extractor(use_llm=not no_llm)
    if warn_no_extractor and isinstance(extractor, DummyExtractor):
        typer.echo(NO_LLM_NOTICE, err=True)
    resolved_concurrency = concurrency if concurrency is not None else config.ingest.concurrency
    return MemoryAgent(
        extractor=extractor,
        embedder=embedder,
        db_path=db,
        ingest_concurrency=resolved_concurrency,
    )
