"""scripts/eval_memory.py — membox Phase 7.5 M3 evaluation harness.

Two modes
---------
``--offline``  (default in CI, no Ollama required)
    Uses :class:`~membox.services.extraction.DummyExtractor` and a custom
    ``SemanticDummyEmbedder`` that returns stable non-zero vectors derived from
    the text hash.  The pipeline runs end-to-end (ingest → query → score) but
    hit rate is **not** checked; the test verifies structural correctness only.

Default mode (Ollama)
    Extracts via ``gemma-4-E2B`` and embeds via ``qwen3-embedding`` (overridable
    through ``MEMBOX_EVAL_*`` env vars), served at ``http://localhost:11434/v1``.  Runs the
    full ingest + scored-query pipeline against the real corpus and reports per-
    question hit/miss and output token estimate.  Exit-nonzero gate is only
    enabled with ``--check-gates`` (hit rate ≥ 0.80 required).

Usage
-----
    # CI smoke test (no Ollama):
    uv run python scripts/eval_memory.py --offline

    # Real evaluation (Ollama must be running):
    uv run python scripts/eval_memory.py

    # With pass/fail gate:
    uv run python scripts/eval_memory.py --check-gates
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Resolve project root and add src/ to path when run as a script.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import yaml  # noqa: E402 — after sys.path adjustment

from membox.config import RetrievalConfig  # noqa: E402
from membox.core.agent import MemoryAgent  # noqa: E402
from membox.model.schema import IngestMetadata  # noqa: E402
from membox.services.extraction import DummyExtractor  # noqa: E402

# ---------------------------------------------------------------------------
# Semantic dummy embedder: stable hash-derived vectors for offline mode.
# ---------------------------------------------------------------------------

_DUMMY_DIM = 16  # small but non-zero; enough for cosine to be meaningful


class _SemanticDummyEmbedder:
    """Deterministic embedder: SHA-256 of text → normalised float32 vector.

    Produces different vectors for different texts (unlike the zero-vector
    DummyEmbedder) so cosine similarity is meaningful in offline testing.
    """

    dim: int = _DUMMY_DIM
    model: str = "semantic_dummy"

    def embed(self, text: str) -> list[float]:
        """Embed text as a normalised hash-derived float32 vector.

        Args:
            text: Input text.

        Returns:
            Normalised float32 vector of length ``self.dim``.
        """
        digest = hashlib.sha256(text.encode()).digest()
        # Unpack the first dim*4 bytes as floats; pad with 1.0 if needed.
        needed = self.dim * 4
        padded = (digest * (needed // len(digest) + 1))[:needed]
        raw = list(struct.unpack(f"{self.dim}f", padded))
        # L2-normalise.
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# ---------------------------------------------------------------------------
# Corpus ingestion
# ---------------------------------------------------------------------------


def ingest_corpus(agent: MemoryAgent, corpus_dir: Path) -> int:
    """Ingest all .md files from corpus_dir into the agent's knowledge store.

    Extraction failures for individual chunks are printed as per-file warnings
    but do not abort the corpus run.  A summary of failed chunks is printed
    at the end.  Silent failure is forbidden: every failure is reported.

    Args:
        agent: Configured MemoryAgent.
        corpus_dir: Directory containing corpus HANDOFF / document files.

    Returns:
        Total number of successfully ingested document chunks.
    """
    total = 0
    total_failed = 0
    for md_file in sorted(corpus_dir.glob("*.md")):
        # Derive project name from filename prefix (e.g. "membox--HANDOFF.md" → "membox").
        project = md_file.stem.split("--")[0]
        results = agent.ingest_file(
            md_file,
            IngestMetadata(project=project, source_path=str(md_file)),
        )
        failed = [r for r in results if "error" in r]
        ok = [r for r in results if "error" not in r]
        if failed:
            for r in failed:
                section = r.get("section") or "(preamble)"
                print(
                    f"  WARNING: extraction failed for {md_file.name}::{section} — {r['error']}",
                    file=sys.stderr,
                )
            total_failed += len(failed)
        total += len(ok)
    if total_failed:
        print(
            f"Corpus ingestion complete: {total} chunks OK, {total_failed} chunks FAILED.",
            file=sys.stderr,
        )
    return total


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def run_evaluation(
    agent: MemoryAgent,
    gold: list[dict[str, Any]],
    budget: int,
    check_gates: bool = False,
    offline: bool = False,
) -> int:
    """Run all gold questions and print per-question results + summary.

    Args:
        agent: Configured MemoryAgent with corpus ingested.
        gold: Parsed gold.yaml question list.
        budget: Token budget passed to compact_query.
        check_gates: If True, exit nonzero when hit rate < 0.8.
        offline: If True, skip hit-rate gate even with --check-gates.

    Returns:
        Exit code (0 for success, 1 if gate fails).
    """
    ret_cfg = RetrievalConfig(budget=budget)
    results_by_cat: dict[str, list[bool]] = {}
    all_token_estimates: list[int] = []

    print(f"{'ID':<6}  {'CAT':<12}  {'HIT?':<5}  {'TOKENS':>6}  QUESTION")
    print("-" * 80)

    for item in gold:
        qid: str = item["id"]
        category: str = item["category"]
        question: str = item["question"]
        expected: list[str] = [kw.lower() for kw in item.get("expected_keywords", [])]

        output = agent.compact_query(question, max_hops=2, config=ret_cfg)

        # Token estimate for this output.
        from membox.core.store.retrieval import est_tokens

        tok_est = est_tokens(output)
        all_token_estimates.append(tok_est)

        # Hit: all expected keywords appear (case-insensitive substring).
        hit = all(kw in output.lower() for kw in expected)

        results_by_cat.setdefault(category, []).append(hit)
        flag = "HIT " if hit else "MISS"
        short_q = question[:50] + ("..." if len(question) > 50 else "")
        print(f"{qid:<6}  {category:<12}  {flag:<5}  {tok_est:>6}  {short_q}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    all_hits = [h for hits in results_by_cat.values() for h in hits]
    overall_rate = sum(all_hits) / len(all_hits) if all_hits else 0.0
    mean_tokens = (
        sum(all_token_estimates) / len(all_token_estimates) if all_token_estimates else 0.0
    )

    print(f"Overall hit rate: {sum(all_hits)}/{len(all_hits)} = {overall_rate:.1%}")
    for cat, hits in sorted(results_by_cat.items()):
        rate = sum(hits) / len(hits) if hits else 0.0
        print(f"  {cat}: {sum(hits)}/{len(hits)} = {rate:.1%}")
    print(f"Mean output tokens: {mean_tokens:.0f}")

    if check_gates and not offline and overall_rate < 0.8:
        print(f"\nGATE FAILED: hit rate {overall_rate:.1%} < 80%", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def make_eval_agent(offline: bool, db_path: str) -> MemoryAgent:
    """Build a MemoryAgent for the evaluation run.

    Args:
        offline: If True, use DummyExtractor + SemanticDummyEmbedder.
        db_path: SQLite database path.

    Returns:
        Configured MemoryAgent.
    """
    if offline:
        extractor = DummyExtractor()
        embedder: object | None = _SemanticDummyEmbedder()

        # _SemanticDummyEmbedder satisfies the Embedder Protocol structurally.
        return MemoryAgent(
            extractor=extractor,
            embedder=embedder,  # type: ignore[arg-type]
            db_path=db_path,
        )

    # Real Ollama mode.
    try:
        from openai import OpenAI
    except ImportError as exc:
        msg = "openai package required for real evaluation: pip install membox[llm]"
        raise SystemExit(msg) from exc

    from membox.providers.openai_compat import OpenAIChatClient, OpenAIEmbedClient
    from membox.services.embedding import EmbeddingService
    from membox.services.extraction import ExtractionService

    base_url = "http://localhost:11434/v1"
    extraction_model = os.environ.get("MEMBOX_EVAL_EXTRACTION_MODEL", "gemma-4-E2B:latest")
    embedding_model = os.environ.get("MEMBOX_EVAL_EMBEDDING_MODEL", "qwen3-embedding:latest")
    embed_dim = int(os.environ.get("MEMBOX_EVAL_EMBED_DIM", "1024"))
    # Calibrated for qwen3-embedding on entity-name pairs (2026-06-10):
    # same-entity cosine >= 0.763, different-entity <= 0.680 -> midpoint 0.72.
    disambiguation_threshold = float(os.environ.get("MEMBOX_EVAL_DISAMBIG_THRESHOLD", "0.72"))

    client = OpenAI(base_url=base_url, api_key="ollama")
    extractor = ExtractionService(
        OpenAIChatClient(client, extraction_model, max_completion_tokens=2048)
    )  # type: ignore[assignment]
    embedder = EmbeddingService(OpenAIEmbedClient(client, embedding_model, embed_dim), embed_dim)
    embedder.model = embedding_model  # type: ignore[attr-defined]

    return MemoryAgent(
        extractor=extractor,
        embedder=embedder,
        db_path=db_path,
        disambiguation_threshold=disambiguation_threshold,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the evaluation harness.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(
        description="Membox M3 evaluation harness — ingest corpus and run gold QA."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use DummyExtractor + SemanticDummyEmbedder; skip hit-rate gate.",
    )
    parser.add_argument(
        "--check-gates",
        action="store_true",
        help="Exit nonzero if hit rate < 80%% (only meaningful in default Ollama mode).",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=2000,
        help="Token budget for compact query output (default: 2000).",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=_REPO_ROOT / "eval" / "corpus",
        help="Directory containing corpus .md files.",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=_REPO_ROOT / "eval" / "gold.yaml",
        help="Path to gold.yaml QA file.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="SQLite database path (default: a temporary file per run).",
    )
    args = parser.parse_args()

    gold_path: Path = args.gold
    corpus_dir: Path = args.corpus_dir

    if not gold_path.exists():
        print(f"ERROR: gold.yaml not found at {gold_path}", file=sys.stderr)
        return 1
    if not corpus_dir.is_dir():
        print(f"ERROR: corpus dir not found at {corpus_dir}", file=sys.stderr)
        return 1

    with open(gold_path) as f:
        gold: list[dict[str, Any]] = yaml.safe_load(f) or []

    # Use a temp DB per run unless --db is specified.
    if args.db:
        db_path = args.db
        agent = make_eval_agent(offline=args.offline, db_path=db_path)
        print(f"Ingesting corpus from {corpus_dir} …")
        total_chunks = ingest_corpus(agent, corpus_dir)
        print(f"Ingested {total_chunks} chunks.\n")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "eval.db")
            agent = make_eval_agent(offline=args.offline, db_path=db_path)
            print(f"Ingesting corpus from {corpus_dir} …")
            total_chunks = ingest_corpus(agent, corpus_dir)
            print(f"Ingested {total_chunks} chunks.\n")
            return run_evaluation(
                agent,
                gold,
                budget=args.budget,
                check_gates=args.check_gates,
                offline=args.offline,
            )

    return run_evaluation(
        agent,
        gold,
        budget=args.budget,
        check_gates=args.check_gates,
        offline=args.offline,
    )


if __name__ == "__main__":
    sys.exit(main())
