"""Shared helpers for membox CLI commands: console, agent factory, notices."""

from __future__ import annotations

import typer
from rich.console import Console

from membox.config import MemboxConfig
from membox.core.agent import MemoryAgent
from membox.services.extraction import DummyExtractor, create_default_extractor

console = Console()

NO_LLM_NOTICE = (
    "No OPENAI_API_KEY / openai package — using no-op extractor; nothing will be extracted."
)


def make_agent(
    db: str,
    no_llm: bool = False,
    warn: bool = False,
    concurrency: int | None = None,
) -> MemoryAgent:
    """Create a MemoryAgent using the best available extraction backend.

    Args:
        db: Path to the SQLite database file.
        no_llm: Force the no-op Dummy backend even if OpenAI is available.
        warn: Print a notice to stderr when the no-op backend is active.
        concurrency: Optional override for per-chunk extraction+embedding
            parallelism.  When None, falls back to the value from
            ``MemboxConfig().ingest.concurrency`` (which itself defaults to
            ``MEMBOX_INGEST_CONCURRENCY`` or 1).  Values below 1 are clamped
            to 1 by ``MemoryAgent``.

    Returns:
        Configured MemoryAgent.
    """
    config = MemboxConfig()
    extractor, embedder = create_default_extractor(use_llm=not no_llm)
    if warn and isinstance(extractor, DummyExtractor):
        typer.echo(NO_LLM_NOTICE, err=True)
    resolved_concurrency = concurrency if concurrency is not None else config.ingest.concurrency
    return MemoryAgent(
        extractor=extractor,
        embedder=embedder,
        db_path=db,
        ingest_concurrency=resolved_concurrency,
    )
