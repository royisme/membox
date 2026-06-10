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
        self.store = KnowledgeStore(db_path)
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
            self.store.upsert_relation(sid, tid, normalize_predicate(rel.predicate), doc_id)
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
        ``project`` defaults to the name of the file's parent directory when not
        provided by the caller.  The CLI ``--project`` option overrides this.

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
        effective_project = metadata.project or resolved.parent.name
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

    def query(self, question: str, max_hops: int = 2) -> str:
        """Query the knowledge graph and return a structured prompt context string.

        Args:
            question: Natural language question.
            max_hops: Maximum BFS hops from seed entities.

        Returns:
            Formatted context string suitable for inclusion in an LLM prompt.
        """
        seeds = self._extractor.extract_query_entities(question)
        result = self.retrieve(seeds, max_hops)
        return self.to_prompt_context(result)

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
