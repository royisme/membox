"""membox agent — MemoryAgent orchestration layer."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from membox.core.chunking import chunk_markdown
from membox.core.normalize import normalize_name, normalize_predicate
from membox.core.store import KnowledgeStore
from membox.model.schema import (
    DocumentChunk,
    Entity,
    ExtractedGraph,
    HopResult,
    IngestMetadata,
    Relation,
)

if TYPE_CHECKING:
    from membox.services.embedding import Embedder
    from membox.services.extraction import LLMExtractor


class MemoryAgent:
    """Orchestrates extraction, normalization, entity resolution, storage, and retrieval.

    The agent is the primary interface for ingesting documents and querying the
    knowledge graph. It delegates storage to KnowledgeStore and extraction to
    an injectable LLMExtractor.
    """

    def __init__(
        self,
        extractor: LLMExtractor,
        embedder: Embedder | None = None,
        db_path: str = "memory.db",
    ) -> None:
        # Pass embedder to KnowledgeStore so the meta guard can run on open.
        self.store = KnowledgeStore(db_path, embedder=embedder)
        self._extractor = extractor
        self._embedder = embedder

    def ingest(self, text: str, source: str = "") -> None:
        """Extract entities and relations from text via LLM and store them.

        Args:
            text: Document text to ingest.
            source: Optional source identifier (file path, URL, etc.).
        """
        graph = self._extractor.extract(text)
        self.ingest_extracted(text, graph, source=source)

    def ingest_extracted(
        self,
        text: str,
        graph: ExtractedGraph,
        source: str = "",
        *,
        project: str | None = None,
        source_path: str | None = None,
        section: str | None = None,
        doc_date: str | None = None,
    ) -> dict[str, int]:
        """Bypass LLM extraction and ingest a pre-built graph. Primary entry point for tests.

        Args:
            text: Original document text (stored for evidence lineage).
            graph: Pre-extracted entities and relations.
            source: Optional source identifier.
            project: Repository / directory name for ``--project`` scoping.
            source_path: Canonical file path; drives automatic version numbering
                on re-ingest.
            section: Section heading if text was chunked from a larger document.
            doc_date: ISO-8601 date string of the document snapshot.

        Returns:
            Dict with doc_id, entities count, and relations count.
        """
        doc_id = self.store.insert_document(
            text,
            source,
            project=project,
            source_path=source_path,
            section=section,
            doc_date=doc_date,
        )
        name_to_id: dict[str, int] = {}
        for entity in graph.entities:
            eid = self.store.find_or_create_entity(
                entity.name, entity.type, entity.description, self._embedder
            )
            name_to_id[entity.name] = eid
        rel_count = 0
        for rel in graph.relations:
            sid = name_to_id.get(rel.source)
            if sid is None:
                sid = self.store.find_or_create_entity(rel.source, "Unknown", "", self._embedder)
            tid = name_to_id.get(rel.target)
            if tid is None:
                tid = self.store.find_or_create_entity(rel.target, "Unknown", "", self._embedder)
            norm_pred = normalize_predicate(rel.predicate)
            # Compute triple embedding once at ingest time (spec §3.7).
            # Rendered as "subject predicate object" plain text.
            rel_embedding: list[float] | None = None
            if self._embedder is not None:
                src_row = self.store.get_entity(sid)
                tgt_row = self.store.get_entity(tid)
                src_str = src_row[1] if src_row else rel.source
                tgt_str = tgt_row[1] if tgt_row else rel.target
                triple_text = f"{src_str} {norm_pred} {tgt_str}"
                rel_embedding = self._embedder.embed(triple_text)
            self.store.upsert_relation(sid, tid, norm_pred, doc_id, embedding=rel_embedding)
            rel_count += 1
        return {"doc_id": doc_id, "entities": len(graph.entities), "relations": rel_count}

    def ingest_file(
        self,
        file_path: Path,
        metadata: IngestMetadata | None = None,
    ) -> list[dict[str, int]]:
        """Ingest a file, chunking markdown documents by ``##`` section headings.

        Each markdown section is extracted separately so entity/relation context
        is tightly scoped to the section.  Non-markdown files are ingested as a
        single document.  The ``source_path`` drives idempotent re-ingest
        versioning: re-ingesting the same file creates new document rows at the
        next version number; old rows are never deleted.

        Judgment call (spec does not specify default for ``project``):
        ``project`` defaults to the name of the nearest git repository root
        directory found by walking up from the file's location (see
        ``_infer_project``).  This correctly handles files inside subdirectories
        such as ``docs/HANDOFF.md`` — they map to the repo name, not ``"docs"``.
        Falls back to the file's parent directory name when no ``.git`` entry is
        found anywhere in the directory hierarchy.  The CLI ``--project`` option
        always overrides this default.

        Args:
            file_path: Path to the file to ingest.  Must exist and be readable.
            metadata: Optional metadata overrides (project, source_path,
                doc_date).  If ``source_path`` is omitted, the resolved absolute
                path of ``file_path`` is used.  If ``doc_date`` is omitted, the
                file's mtime date (ISO-8601) is used.

        Returns:
            List of per-chunk ingest result dicts (doc_id, entities, relations).

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        resolved = file_path.resolve()
        if not resolved.exists():
            msg = f"File not found: {file_path}"
            raise FileNotFoundError(msg)

        if metadata is None:
            metadata = IngestMetadata()

        effective_source_path = metadata.source_path or str(resolved)
        effective_project = metadata.project or _infer_project(resolved)
        effective_doc_date = metadata.doc_date or _file_mtime_date(resolved)

        content = resolved.read_text(encoding="utf-8")
        suffix = resolved.suffix.lower()

        if suffix in {".md", ".markdown"}:
            raw_chunks = chunk_markdown(content)
        else:
            raw_chunks = [(None, content)]

        results: list[dict[str, int]] = []
        for section_title, chunk_content in raw_chunks:
            if not chunk_content.strip():
                continue
            chunk = DocumentChunk(section=section_title, content=chunk_content)
            graph = self._extractor.extract(chunk.content)
            result = self.ingest_extracted(
                chunk.content,
                graph,
                source=effective_source_path,
                project=effective_project,
                source_path=effective_source_path,
                section=chunk.section,
                doc_date=effective_doc_date,
            )
            results.append(result)

        return results

    def query(
        self,
        question: str,
        max_hops: int = 2,
        budget: int | None = None,
        project_filter: str | None = None,
    ) -> str:
        """Query the knowledge graph and return a compact context string.

        Always uses the compact subject-grouped output format with token-budget
        truncation (spec §3.7).  The budget defaults to
        ``config.retrieval.budget`` (2000) when not explicitly supplied.

        Args:
            question: Natural language question.
            max_hops: Maximum BFS hops from seed entities.
            budget: Token budget override.  ``None`` uses the config default
                (``retrieval.budget``, 2000).
            project_filter: Restrict evidence to this project name.

        Returns:
            Compact context string with coverage footer.
        """
        return self.compact_query(
            question,
            max_hops=max_hops,
            budget=budget,
            project_filter=project_filter,
        )

    def compact_query(
        self,
        question: str,
        max_hops: int = 2,
        budget: int | None = None,
        project_filter: str | None = None,
        config: object | None = None,
    ) -> str:
        """Hybrid BFS + scored rerank + compact output with token-budget truncation.

        Implements the full spec §3.7 pipeline:
        1. Resolve seed entities (alias → embedding similarity).
        2. BFS with depth tracking.
        3. FTS5 BM25 scoring + cosine sim scoring via stored relation embeddings.
        4. Composite score: ``decay^hops * (a*sim + (1-a)*bm25)``.
        5. Greedy best-effort knapsack within ``budget`` tokens.
        6. Compact subject-grouped output with provenance tags and coverage footer.

        Args:
            question: Natural language question.
            max_hops: Maximum BFS hops.
            budget: Token budget; defaults to ``config.retrieval.budget`` (2000).
            project_filter: Restrict evidence to this project name.
            config: :class:`~membox.config.MemboxConfig` or
                :class:`~membox.config.RetrievalConfig`; uses defaults if None.

        Returns:
            Compact context string with coverage footer.
        """
        from membox.config import MemboxConfig, RetrievalConfig

        if isinstance(config, MemboxConfig):
            ret_cfg = config.retrieval
        elif isinstance(config, RetrievalConfig):
            ret_cfg = config
        else:
            ret_cfg = RetrievalConfig()

        effective_budget = budget if budget is not None else ret_cfg.budget

        seeds = self._extractor.extract_query_entities(question)
        seed_ids: list[int] = []
        for name in seeds:
            eid = self.store.find_entity_by_alias(normalize_name(name))
            if eid is None and self._embedder is not None:
                emb = self._embedder.embed(name)
                eid = self.store.find_similar_entity(emb, None)
            if eid is not None:
                seed_ids.append(eid)

        if not seed_ids:
            return "(returned 0/0 triples, ~0/0 tokens)"

        query_emb: list[float] | None = None
        if self._embedder is not None:
            query_emb = self._embedder.embed(question)

        scored = self.store.scored_query(
            seed_ids=seed_ids,
            max_hops=max_hops,
            query=question,
            query_embedding=query_emb,
            config=ret_cfg,
            project_filter=project_filter,
        )

        return self.store.compact_output(
            scored=scored,
            budget=effective_budget,
            top_evidence_k=ret_cfg.top_evidence_k,
        )

    def retrieve(self, seed_names: list[str], max_hops: int = 2) -> HopResult:
        """Resolve seed names to entity IDs and BFS-expand the graph.

        Seed resolution order: exact alias match → embedding similarity (if embedder
        is available). Seeds that cannot be resolved are silently skipped.

        Args:
            seed_names: Entity name strings to use as BFS starting points.
            max_hops: Maximum BFS hops.

        Returns:
            HopResult with traversal data; empty if no seeds resolve.
        """
        seed_ids: list[int] = []
        for name in seed_names:
            eid = self.store.find_entity_by_alias(normalize_name(name))
            if eid is None and self._embedder is not None:
                emb = self._embedder.embed(name)
                eid = self.store.find_similar_entity(emb, None)
            if eid is not None:
                seed_ids.append(eid)
        if not seed_ids:
            return HopResult(seed_names=seed_names)
        result = self.store.bfs_query(seed_ids, max_hops)
        return HopResult(
            triplets=result.triplets,
            documents=result.documents,
            seed_names=seed_names,
            visited_entities=result.visited_entities,
        )

    def to_prompt_context(self, result: HopResult, max_docs: int = 5) -> str:
        """Format a HopResult as a structured prompt context string.

        Args:
            result: BFS retrieval result.
            max_docs: Maximum number of evidence documents to include.

        Returns:
            Multi-line string with knowledge topology and source citations.
        """
        if not result.triplets and not result.documents:
            return "本地记忆库中没有找到相关背景信息。"
        lines = ["【知识拓扑】"]
        for s, p, o in result.triplets:
            lines.append(f"[{s}] --({p})--> [{o}]")
        lines.append("")
        lines.append("【上下文溯源】")
        lines.extend(f"- {doc}" for doc in result.documents[:max_docs])
        return "\n".join(lines)

    def list_entities(self) -> list[Entity]:
        """Return all entities in the knowledge graph.

        Returns:
            List of Entity objects.
        """
        return self.store.list_entities()

    def list_relations(self) -> list[Relation]:
        """Return all relations in the knowledge graph.

        Returns:
            List of Relation objects with source_name and target_name resolved.
        """
        return self.store.list_relations()


def _file_mtime_date(path: Path) -> str:
    """Return the file modification date as an ISO-8601 date string (YYYY-MM-DD).

    Args:
        path: Resolved, existing file path.

    Returns:
        Date string in ``YYYY-MM-DD`` format derived from the file's mtime.
    """
    mtime = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).strftime("%Y-%m-%d")


def _infer_project(path: Path) -> str:
    """Infer the project name for a file by walking up to the nearest git root.

    Walks upward from *path*'s directory looking for a ``.git`` entry (which
    may be a directory in a normal clone or a file in a git worktree).  If a
    git root is found its directory name is returned; otherwise the immediate
    parent directory name is used as a fallback.

    Args:
        path: Resolved absolute path to the file being ingested.

    Returns:
        The name of the git repository root directory, or the file's immediate
        parent directory name when no ``.git`` entry is found up to the
        filesystem root.

    Examples:
        >>> _infer_project(Path("/home/user/myrepo/docs/HANDOFF.md"))
        'myrepo'  # git root found at /home/user/myrepo
        >>> _infer_project(Path("/tmp/scratch/note.txt"))
        'scratch'  # no git root found, falls back to parent dir
    """
    current = path.parent
    while True:
        if (current / ".git").exists():
            return current.name
        parent = current.parent
        if parent == current:
            # Reached the filesystem root without finding .git.
            break
        current = parent
    return path.parent.name
