"""membox agent — MemoryAgent orchestration layer."""

from __future__ import annotations

import datetime
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from membox.core.chunking import _DEFAULT_MAX_TOKENS, chunk_markdown
from membox.core.normalize import normalize_name, normalize_predicate
from membox.core.project import infer_project
from membox.core.store import KnowledgeStore
from membox.core.store.retrieval import est_tokens
from membox.model.schema import (
    DocumentChunk,
    Entity,
    ExtractedGraph,
    HopResult,
    IngestMetadata,
    Relation,
)
from membox.services.embedding import BatchEmbedder

if TYPE_CHECKING:
    from membox.config import RetrievalConfig
    from membox.core.store.memory_units import MemoryQueryHit
    from membox.services.embedding import Embedder
    from membox.services.extraction import LLMExtractor


def _prewarm_embeddings(embedder: Embedder | None, texts: list[str]) -> None:
    """Populate a batch-capable embedder's cache for unique texts."""
    if embedder is None or not isinstance(embedder, BatchEmbedder):
        return
    unique_texts = list(dict.fromkeys(texts))
    if unique_texts:
        embedder.embed_many(unique_texts)


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
        *,
        disambiguation_threshold: float = 0.85,
        ingest_concurrency: int = 1,
    ) -> None:
        # Pass embedder to KnowledgeStore so the meta guard can run on open.
        self.store = KnowledgeStore(db_path, embedder=embedder)
        self._extractor = extractor
        self._embedder = embedder
        self._disambiguation_threshold = disambiguation_threshold
        self._ingest_concurrency = max(1, ingest_concurrency)

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
        threshold = self._disambiguation_threshold
        name_to_id: dict[str, int] = {}
        entity_specs = {entity.name: (entity.type, entity.description) for entity in graph.entities}
        for rel in graph.relations:
            entity_specs.setdefault(rel.source, ("Unknown", ""))
            entity_specs.setdefault(rel.target, ("Unknown", ""))

        _prewarm_embeddings(self._embedder, list(entity_specs))

        for name, (type_, description) in entity_specs.items():
            eid = self.store.find_or_create_entity(
                name, type_, description, self._embedder, threshold=threshold
            )
            name_to_id[name] = eid
        rel_count = 0
        relation_payloads: list[tuple[int, int, str, str]] = []
        for rel in graph.relations:
            sid = name_to_id[rel.source]
            tid = name_to_id[rel.target]
            norm_pred = normalize_predicate(rel.predicate)
            src_row = self.store.get_entity(sid)
            tgt_row = self.store.get_entity(tid)
            src_str = src_row[1] if src_row else rel.source
            tgt_str = tgt_row[1] if tgt_row else rel.target
            triple_text = f"{src_str} {norm_pred} {tgt_str}"
            relation_payloads.append((sid, tid, norm_pred, triple_text))

        _prewarm_embeddings(self._embedder, [payload[3] for payload in relation_payloads])

        for sid, tid, norm_pred, triple_text in relation_payloads:
            # Compute triple embedding once at ingest time (spec §3.7).
            # Rendered as "subject predicate object" plain text.
            rel_embedding: list[float] | None = None
            if self._embedder is not None:
                rel_embedding = self._embedder.embed(triple_text)
            self.store.upsert_relation(sid, tid, norm_pred, doc_id, embedding=rel_embedding)
            rel_count += 1
        return {"doc_id": doc_id, "entities": len(graph.entities), "relations": rel_count}

    def ingest_content(
        self,
        content: str,
        *,
        source: str = "",
        project: str | None = None,
        source_path: str | None = None,
        doc_date: str | None = None,
        markdown: bool | None = None,
        chunk_max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> list[dict[str, int | str]]:
        """Run the full chunk → extract → embed → store pipeline on raw text.

        This is the synchronous materialization primitive shared by
        :meth:`ingest_file` (direct path) and the M6 queue worker (deferred
        path).  Markdown content is chunked on ``##`` section boundaries;
        per-chunk extraction failures are isolated into the result list.

        Args:
            content: Raw document text.
            source: Legacy source identifier stored on each document row.
            project: Repository / project name for scoping.
            source_path: Canonical file path; drives re-ingest versioning.
            doc_date: ISO-8601 date string of the document snapshot.
            markdown: Force markdown chunking on/off.  ``None`` infers from
                the ``source_path`` suffix (``.md`` / ``.markdown``).
            chunk_max_tokens: Maximum estimated tokens per chunk before
                paragraph-level sub-chunking is applied.
            Concurrent extraction is controlled by this instance's
                ``ingest_concurrency`` setting; SQLite writes remain serial
                and preserve chunk order.

        Returns:
            List of per-chunk result dicts.  Successful chunks have keys
            ``doc_id``, ``entities``, and ``relations``.  Failed chunks have
            keys ``section`` and ``error``.
        """
        if markdown is None:
            suffix = Path(source_path).suffix.lower() if source_path else ""
            markdown = suffix in {".md", ".markdown"}

        if markdown:
            raw_chunks = chunk_markdown(content, max_tokens=chunk_max_tokens)
        else:
            raw_chunks = [(None, content)]

        chunks = [
            DocumentChunk(section=section_title, content=chunk_content)
            for section_title, chunk_content in raw_chunks
            if chunk_content.strip()
        ]

        if self._ingest_concurrency <= 1 or len(chunks) <= 1:
            return [
                self._extract_and_ingest_chunk(
                    chunk,
                    source=source,
                    project=project,
                    source_path=source_path,
                    doc_date=doc_date,
                )
                for chunk in chunks
            ]

        results: list[dict[str, int | str]] = []
        with ThreadPoolExecutor(max_workers=self._ingest_concurrency) as executor:
            futures = [executor.submit(self._extractor.extract, chunk.content) for chunk in chunks]
            for chunk, future in zip(chunks, futures, strict=True):
                try:
                    graph = future.result()
                    _ok = self.ingest_extracted(
                        chunk.content,
                        graph,
                        source=source,
                        project=project,
                        source_path=source_path,
                        section=chunk.section,
                        doc_date=doc_date,
                    )
                    result: dict[str, int | str] = dict(_ok)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # broad catch is intentional at orchestration layer
                    result = {
                        "section": chunk.section or "",
                        "error": str(exc),
                    }
                results.append(result)

        return results

    def _extract_and_ingest_chunk(
        self,
        chunk: DocumentChunk,
        *,
        source: str,
        project: str | None,
        source_path: str | None,
        doc_date: str | None,
    ) -> dict[str, int | str]:
        """Extract and materialize one chunk, isolating non-interrupt failures."""
        try:
            graph = self._extractor.extract(chunk.content)
            _ok = self.ingest_extracted(
                chunk.content,
                graph,
                source=source,
                project=project,
                source_path=source_path,
                section=chunk.section,
                doc_date=doc_date,
            )
            return dict(_ok)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # broad catch is intentional at orchestration layer
            return {
                "section": chunk.section or "",
                "error": str(exc),
            }

    def ingest_file(
        self,
        file_path: Path,
        metadata: IngestMetadata | None = None,
        chunk_max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> list[dict[str, int | str]]:
        """Ingest a file, chunking markdown documents by ``##`` section headings.

        Each markdown section is extracted separately so entity/relation context
        is tightly scoped to the section.  Non-markdown files are ingested as a
        single document.  The ``source_path`` drives idempotent re-ingest
        versioning: re-ingesting the same file creates new document rows at the
        next version number; old rows are never deleted.

        Sections whose estimated token count exceeds *chunk_max_tokens* are
        further split on paragraph boundaries before extraction (see
        :func:`~membox.core.chunking.chunk_markdown`).

        Extraction failures for individual chunks are caught and recorded in
        the result list rather than propagating.  The caller inspects the
        returned list for entries that contain an ``"error"`` key.
        ``KeyboardInterrupt`` is never caught and always propagates immediately.

        Judgment call (spec does not specify default for ``project``):
        ``project`` defaults to the name of the nearest git repository root
        directory found by walking up from the file's location (see
        ``infer_project``).  This correctly handles files inside subdirectories
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
            chunk_max_tokens: Maximum estimated tokens per chunk before
                paragraph-level sub-chunking is applied.  Passed directly to
                :func:`~membox.core.chunking.chunk_markdown`.

        Returns:
            List of per-chunk result dicts.  Successful chunks have keys
            ``doc_id``, ``entities``, and ``relations``.  Failed chunks have
            keys ``section`` (section title, may be ``None``) and ``error``
            (string representation of the exception).

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
        effective_project = metadata.project or infer_project(resolved)
        effective_doc_date = metadata.doc_date or _file_mtime_date(resolved)

        content = resolved.read_text(encoding="utf-8")

        return self.ingest_content(
            content,
            source=effective_source_path,
            project=effective_project,
            source_path=effective_source_path,
            doc_date=effective_doc_date,
            markdown=resolved.suffix.lower() in {".md", ".markdown"},
            chunk_max_tokens=chunk_max_tokens,
        )

    def enqueue(
        self,
        content: str,
        *,
        project: str | None = None,
        source_path: str | None = None,
        doc_date: str | None = None,
    ) -> int:
        """Accept a document for deferred ingestion (spec §3.9 fast path).

        Performs a single SQLite INSERT — no chunking, no LLM calls — and
        returns immediately.  Materialization happens when a worker drains the
        queue (``membox process`` or the auto-spawned worker subprocess).

        Args:
            content: Raw document text.
            project: Repository / project name captured at enqueue time.
            source_path: Canonical file path captured at enqueue time.
            doc_date: ISO-8601 date string captured at enqueue time.

        Returns:
            Queue row id.
        """
        return self.store.enqueue_ingest(
            content,
            project=project,
            source_path=source_path,
            doc_date=doc_date,
        )

    def enqueue_file(
        self,
        file_path: Path,
        metadata: IngestMetadata | None = None,
    ) -> int:
        """Read a file and enqueue its content for deferred ingestion.

        Metadata defaults mirror :meth:`ingest_file` (project inferred from
        the nearest git root, doc_date from mtime) so the worker materializes
        identical document rows to a synchronous ingest.

        Args:
            file_path: Path to the file to enqueue.  Must exist.
            metadata: Optional metadata overrides (project, source_path,
                doc_date).

        Returns:
            Queue row id.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        resolved = file_path.resolve()
        if not resolved.exists():
            msg = f"File not found: {file_path}"
            raise FileNotFoundError(msg)
        if metadata is None:
            metadata = IngestMetadata()
        return self.enqueue(
            resolved.read_text(encoding="utf-8"),
            project=metadata.project or infer_project(resolved),
            source_path=metadata.source_path or str(resolved),
            doc_date=metadata.doc_date or _file_mtime_date(resolved),
        )

    def query(
        self,
        question: str,
        max_hops: int = 2,
        budget: int | None = None,
        project_filter: str | None = None,
        *,
        include_superseded: bool = False,
        include_memory: bool = False,
        memory_project: str | None = None,
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
            include_superseded: When True, superseded relations are included in
                the BFS traversal.  Defaults to False.
            include_memory: When True, append opt-in query-side memory recall.
            memory_project: Project scope for memory recall; None means all
                projects and should only be used for explicit cross-project calls.

        Returns:
            Compact context string with coverage footer.
        """
        return self.compact_query(
            question,
            max_hops=max_hops,
            budget=budget,
            project_filter=project_filter,
            include_superseded=include_superseded,
            include_memory=include_memory,
            memory_project=memory_project,
        )

    def compact_query(
        self,
        question: str,
        max_hops: int = 2,
        budget: int | None = None,
        project_filter: str | None = None,
        config: object | None = None,
        *,
        include_superseded: bool = False,
        include_memory: bool = False,
        memory_project: str | None = None,
        _append_pending: bool = True,
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
            include_superseded: When True, superseded relations are included in
                the BFS traversal.  Defaults to False.
            include_memory: When True, append opt-in memory units/crystals under
                a separate budget partition.
            memory_project: Project scope for memory recall. Pass None only for
                explicit all-project memory recall.

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

        if include_memory:
            return self._compact_query_with_memory(
                question,
                max_hops=max_hops,
                budget=effective_budget,
                project_filter=project_filter,
                ret_cfg=ret_cfg,
                include_superseded=include_superseded,
                memory_project=memory_project,
            )

        seeds = self._extractor.extract_query_entities(question)
        seed_ids: list[int] = []
        for name in seeds:
            eid = self.store.find_entity_by_alias(normalize_name(name))
            if eid is None and self._embedder is not None:
                emb = self._embedder.embed(name)
                eid = self.store.find_similar_entity(emb, None)
            if eid is not None:
                seed_ids.append(eid)

        if ret_cfg.fusion_mode == "fallback":
            # Original either/or control flow: preserved for A/B comparison and rollback.
            if not seed_ids:
                fallback = self._fts_fallback(question, effective_budget, project_filter, ret_cfg)
                return self._append_pending_note(fallback) if _append_pending else fallback

            query_emb_fb: list[float] | None = None
            if self._embedder is not None:
                query_emb_fb = self._embedder.embed(question)

            scored_fb = self.store.scored_query(
                seed_ids=seed_ids,
                max_hops=max_hops,
                query=question,
                query_embedding=query_emb_fb,
                config=ret_cfg,
                project_filter=project_filter,
                include_superseded=include_superseded,
            )

            if not scored_fb:
                fallback = self._fts_fallback(question, effective_budget, project_filter, ret_cfg)
                return self._append_pending_note(fallback) if _append_pending else fallback

            output_fb = self.store.compact_output(
                scored=scored_fb,
                budget=effective_budget,
                top_evidence_k=ret_cfg.top_evidence_k,
            )
            return self._append_pending_note(output_fb) if _append_pending else output_fb

        # --- fusion_mode == "merge": budget-partitioned graph+FTS fusion ---
        # Seed resolution (same as before).
        query_emb: list[float] | None = None
        if self._embedder is not None:
            query_emb = self._embedder.embed(question)

        # Triple pool: BFS + scoring (empty when seed_ids is empty).
        if seed_ids:
            scored = self.store.scored_query(
                seed_ids=seed_ids,
                max_hops=max_hops,
                query=question,
                query_embedding=query_emb,
                config=ret_cfg,
                project_filter=project_filter,
                include_superseded=include_superseded,
            )
        else:
            scored = []

        # Chunk pool: always fetch unless fts_fallback_k <= 0.
        if ret_cfg.fts_fallback_k > 0:
            chunks = self.store.fts_fallback_chunks(
                question,
                limit=ret_cfg.fts_fallback_k,
                project_filter=project_filter,
            )
        else:
            chunks = []

        output = self.store.fused_output(
            scored=scored,
            chunks=chunks,
            budget=effective_budget,
            chunk_share=ret_cfg.chunk_share,
            top_evidence_k=ret_cfg.top_evidence_k,
        )
        return self._append_pending_note(output) if _append_pending else output

    def _compact_query_with_memory(
        self,
        question: str,
        *,
        max_hops: int,
        budget: int,
        project_filter: str | None,
        ret_cfg: RetrievalConfig,
        include_superseded: bool,
        memory_project: str | None,
    ) -> str:
        """Return compact query output with an opt-in memory section."""
        memory_budget = int(budget * ret_cfg.memory_share)
        memory_hits = self.store.search_memory_units_for_query(
            memory_project,
            question,
            limit=20,
        )
        (
            memory_lines,
            memory_used,
            admitted_crystals,
            total_crystals,
            admitted_units,
            total_units,
            admitted_ids,
        ) = self._render_memory_hits(memory_hits, memory_budget)
        semantic_budget = budget - memory_budget + (memory_budget - memory_used)
        base_output = self.compact_query(
            question,
            max_hops=max_hops,
            budget=semantic_budget,
            project_filter=project_filter,
            config=ret_cfg,
            include_superseded=include_superseded,
            _append_pending=False,
        )
        output = self._insert_memory_section(
            base_output,
            memory_lines,
            memory_used=memory_used,
            total_budget=budget,
            admitted_crystals=admitted_crystals,
            total_crystals=total_crystals,
            admitted_units=admitted_units,
            total_units=total_units,
        )
        with suppress(Exception):
            self.store.mark_memory_units_recalled(admitted_ids)
        return self._append_pending_note(output)

    def _render_memory_hits(
        self,
        hits: list[MemoryQueryHit],
        budget: int,
    ) -> tuple[list[str], int, int, int, int, int, list[int]]:
        """Render memory hits within a budget and return coverage counts.

        Admission is rank-prefix: rendering stops at the first hit that does
        not fit the remaining budget, so admitted hits are always the strict
        top-N of the ranked pool and the footer's ``n/m`` coverage reads as
        "best n of m". Lower-ranked shorter hits never leapfrog a higher-ranked
        one that was skipped for size (owner decision #3: crystals-before-units
        applies to ranking, and admission must not reorder it).
        """
        from membox.core.consolidate import detect_conflicts

        remaining = budget
        lines: list[str] = []
        admitted_crystals = 0
        admitted_units = 0
        total_crystals = sum(1 for hit in hits if hit["status"] == "crystal")
        total_units = len(hits) - total_crystals
        admitted_ids: list[int] = []
        records = [self.store.get_memory_unit(hit["id"]) for hit in hits]
        conflict_ids = {
            unit_id
            for conflict in detect_conflicts([record for record in records if record is not None])
            for unit_id in (conflict.left_id, conflict.right_id)
        }
        for hit in hits:
            tier = "crystal" if hit["status"] == "crystal" else "unit"
            content = str(hit["content"]).strip().splitlines()[0] if hit["content"].strip() else ""
            conflict_prefix = "[conflict] " if hit["id"] in conflict_ids else ""
            line = f"{conflict_prefix}[{tier}] {hit['unit_type']}: {hit['title']} - {content}"
            cost = est_tokens(line)
            if cost > remaining:
                break
            lines.append(line)
            remaining -= cost
            if tier == "crystal":
                admitted_crystals += 1
            else:
                admitted_units += 1
            admitted_ids.append(hit["id"])
        return (
            lines,
            budget - remaining,
            admitted_crystals,
            total_crystals,
            admitted_units,
            total_units,
            admitted_ids,
        )

    def _insert_memory_section(
        self,
        output: str,
        memory_lines: list[str],
        *,
        memory_used: int,
        total_budget: int,
        admitted_crystals: int,
        total_crystals: int,
        admitted_units: int,
        total_units: int,
    ) -> str:
        """Insert memory lines before the coverage footer and extend the footer."""
        lines = output.splitlines()
        footer_index = next(
            (index for index, line in enumerate(lines) if line.startswith("(returned ")),
            len(lines),
        )
        before = lines[:footer_index]
        footer = lines[footer_index] if footer_index < len(lines) else "(returned 0/0 triples)"
        after = lines[footer_index + 1 :] if footer_index < len(lines) else []
        if memory_lines:
            if before and before[-1] != "":
                before.append("")
            before.append("Relevant memory")
            before.extend(memory_lines)
            before.append("")
        new_footer = self._extend_memory_footer(
            footer,
            memory_used=memory_used,
            total_budget=total_budget,
            admitted_crystals=admitted_crystals,
            total_crystals=total_crystals,
            admitted_units=admitted_units,
            total_units=total_units,
        )
        return "\n".join([*before, new_footer, *after])

    def _extend_memory_footer(
        self,
        footer: str,
        *,
        memory_used: int,
        total_budget: int,
        admitted_crystals: int,
        total_crystals: int,
        admitted_units: int,
        total_units: int,
    ) -> str:
        """Append memory coverage to an existing graph/chunk coverage footer."""
        footer_body = footer[:-1] if footer.endswith(")") else footer
        footer_body = re.sub(
            r"~([0-9,]+)/([0-9,]+) tokens",
            lambda match: (
                f"~{int(match.group(1).replace(',', '')) + memory_used:,}/{total_budget:,} tokens"
            ),
            footer_body,
        )
        return (
            f"{footer_body}, {admitted_crystals}/{total_crystals} crystals, "
            f"{admitted_units}/{total_units} units)"
        )

    def _fts_fallback(
        self,
        question: str,
        budget: int,
        project_filter: str | None,
        ret_cfg: RetrievalConfig,
    ) -> str:
        """Direct FTS5 chunk search when graph retrieval comes back empty.

        Seed-resolution failure (no extracted entity matches the graph) and
        empty BFS recall are the dominant miss modes on real corpora; rather
        than returning a bare ``0/0`` footer, search the evidence chunks
        directly so keyword-bearing prose still surfaces (spec §3.6).

        Args:
            question: Original natural-language question.
            budget: Effective token budget for the output.
            project_filter: Restrict chunks to this project name.
            ret_cfg: Resolved retrieval configuration (``fts_fallback_k``).

        Returns:
            Provenance-tagged chunk output with coverage footer, or the bare
            empty footer when the fallback is disabled or finds nothing.
        """
        if ret_cfg.fts_fallback_k <= 0:
            return "(returned 0/0 triples, ~0/0 tokens)"
        chunks = self.store.fts_fallback_chunks(
            question,
            limit=ret_cfg.fts_fallback_k,
            project_filter=project_filter,
        )
        if not chunks:
            return "(returned 0/0 triples, ~0/0 tokens)"
        return self.store.fts_fallback_output(chunks, budget=budget)

    def _append_pending_note(self, output: str) -> str:
        """Append a pending-ingests note to query output when the queue is non-empty.

        Eventual consistency must be observable (spec §3.9): when queue rows
        are still pending or processing, the reader is told results may be
        incomplete instead of silently serving a stale graph.

        Args:
            output: Compact query output ending with the coverage footer.

        Returns:
            Output, with a staleness note appended when applicable.
        """
        pending = self.store.pending_ingest_count()
        if pending == 0:
            return output
        return f"{output}\n({pending} ingest(s) pending — results may be incomplete)"

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


_infer_project = infer_project
