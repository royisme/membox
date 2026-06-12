"""membox config — runtime configuration for LLM provider selection.

``MemboxConfig`` carries one settings group per capability (extraction and
embedding) so each can target a different provider, model, and endpoint.
API keys default to environment variables so configs can be checked in
without secrets.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

_PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "vllm": "VLLM_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_DEFAULT_ENV_VAR = "OPENAI_API_KEY"


class ProviderConfig(BaseModel):
    """Connection settings for one LLM capability (extraction or embedding).

    Attributes:
        provider: Provider family name, e.g. ``openai``, ``deepseek``,
            ``ollama``, ``vllm``. Determines the API-key environment variable.
        model: Model name to request from the provider.
        base_url: Optional API base URL. ``None`` means the provider's
            default endpoint; setting it routes OpenAI-compatible traffic to
            Ollama/vLLM/DeepSeek/etc.
        api_key: Explicit API key. ``None`` defers to the provider's
            environment variable (see :meth:`resolved_api_key`).
    """

    provider: str = "openai"
    model: str
    base_url: str | None = None
    api_key: str | None = None

    def resolved_api_key(self) -> str | None:
        """Return the effective API key for this capability.

        An explicitly configured ``api_key`` wins; otherwise the key is read
        from the provider's conventional environment variable
        (``OPENAI_API_KEY`` for unknown providers).  Ollama does not require
        authentication, so that provider falls back to the placeholder
        ``"ollama"`` (the OpenAI SDK rejects a missing key even when the
        server ignores it).

        Returns:
            API key string, or None if neither config nor environment has one.
        """
        if self.api_key is not None:
            return self.api_key
        env_var = _PROVIDER_ENV_VARS.get(self.provider, _DEFAULT_ENV_VAR)
        key = os.environ.get(env_var)
        if key is None and self.provider == "ollama":
            return "ollama"
        return key


def _env(name: str, default: str) -> str:
    """Read a ``MEMBOX_*`` environment override with a fallback default."""
    return os.environ.get(name, default)


class ExtractionConfig(ProviderConfig):
    """Provider settings for the LLM extraction capability.

    Defaults are environment-overridable (``MEMBOX_EXTRACTION_PROVIDER``,
    ``MEMBOX_EXTRACTION_MODEL``, ``MEMBOX_EXTRACTION_BASE_URL``) so the CLI
    can target Ollama/vLLM/DeepSeek without code changes, e.g.::

        export MEMBOX_EXTRACTION_PROVIDER=ollama
        export MEMBOX_EXTRACTION_MODEL=gemma-4-E2B:latest
        export MEMBOX_EXTRACTION_BASE_URL=http://localhost:11434/v1

    Attributes:
        max_completion_tokens: Maximum number of tokens the model may generate
            in a single response.  ``None`` defers to the server default.
            Set this to a positive integer (e.g. ``2048``) when targeting
            providers with a small context window (e.g. Ollama at 8 192
            tokens) so the prompt + completion cannot overflow the window.
    """

    provider: str = Field(default_factory=lambda: _env("MEMBOX_EXTRACTION_PROVIDER", "openai"))
    model: str = Field(default_factory=lambda: _env("MEMBOX_EXTRACTION_MODEL", "gpt-4o-mini"))
    base_url: str | None = Field(
        default_factory=lambda: os.environ.get("MEMBOX_EXTRACTION_BASE_URL")
    )
    max_completion_tokens: int | None = None


class EmbeddingConfig(ProviderConfig):
    """Provider settings for the embedding capability.

    Defaults are environment-overridable (``MEMBOX_EMBEDDING_PROVIDER``,
    ``MEMBOX_EMBEDDING_MODEL``, ``MEMBOX_EMBEDDING_BASE_URL``,
    ``MEMBOX_EMBEDDING_DIM``), mirroring :class:`ExtractionConfig`.

    Attributes:
        dimensions: Embedding vector dimensionality requested from the API.
        cache_size: Maximum number of process-local embedding vectors cached
            per embedder instance. Set to 0 to disable caching.
        batch_size: Maximum number of cache-miss texts sent in one provider
            embedding request.
    """

    provider: str = Field(default_factory=lambda: _env("MEMBOX_EMBEDDING_PROVIDER", "openai"))
    model: str = Field(
        default_factory=lambda: _env("MEMBOX_EMBEDDING_MODEL", "text-embedding-3-small")
    )
    base_url: str | None = Field(
        default_factory=lambda: os.environ.get("MEMBOX_EMBEDDING_BASE_URL")
    )
    dimensions: int = Field(default_factory=lambda: int(_env("MEMBOX_EMBEDDING_DIM", "1536")))
    cache_size: int = Field(
        default_factory=lambda: int(_env("MEMBOX_EMBEDDING_CACHE_SIZE", "10000"))
    )
    batch_size: int = Field(default_factory=lambda: int(_env("MEMBOX_EMBEDDING_BATCH_SIZE", "128")))


class RetrievalConfig(BaseModel):
    """Settings that govern hybrid retrieval scoring and token-budget truncation.

    Attributes:
        hop_decay: Per-hop attenuation factor applied to the composite score
            (``decay^hops``).  Default ``0.7`` per spec §3.7.
        alpha: Weight of the embedding similarity component in the scoring
            formula.  ``(1 - alpha)`` is the weight for BM25.  Default ``0.6``.
        budget: Token budget for the compact retrieval output (triple lines +
            evidence snippets, excluding the fixed coverage footer).  Default
            ``2000``.
        top_evidence_k: Number of highest-scored triples eligible to have an
            evidence snippet attached.  Default ``3``.
        disambiguation_threshold: Minimum cosine similarity for entity
            disambiguation (embedding-based fuzzy match in
            ``find_or_create_entity``).  Default ``0.85``, which is appropriate
            for OpenAI embeddings.  Set to ``0.70`` when using ``embeddinggemma``
            via Ollama.
        fts_fallback_k: Maximum number of FTS5 chunk candidates fetched for
            the chunk pool (merge mode) or the direct fallback search
            (fallback mode).  This caps the candidate pool only — actual
            output is still bounded by the token budget.  ``0`` disables the
            FTS channel.  Default ``10`` (eval-calibrated: answer-bearing
            chunks ranked 6-8 were cut off at the previous default of 5).
        fusion_mode: Retrieval fusion strategy.  ``"merge"`` (default) runs
            the budget-partitioned graph+FTS fusion (spec §3.6 Step 1):
            both the triple pool and the chunk pool are always fetched and
            their admissions are interleaved across a partitioned token
            budget.  ``"fallback"`` preserves the original either/or control
            flow (graph non-empty → no chunks shown) for A/B comparison and
            one-click rollback.
        chunk_share: Fraction of the token budget reserved for FTS chunks in
            ``"merge"`` mode.  Remaining budget (``1 - chunk_share``) is used
            for triples in pass 1; any leftover rolls over to pass 2 chunks,
            and any chunk leftover rolls back to triple backfill in pass 3.
            Default ``0.4``.
        memory_share: Fraction of the token budget reserved for opt-in
            query-side memory recall when ``--include-memory`` is used.
            Default ``0.15`` per spec_02 Phase E.
    """

    hop_decay: float = 0.7
    alpha: float = 0.6
    budget: int = 2000
    top_evidence_k: int = 3
    disambiguation_threshold: float = 0.85
    fts_fallback_k: int = 10
    fusion_mode: str = "merge"
    chunk_share: float = 0.4
    memory_share: float = 0.15


class HistoryConfig(BaseModel):
    """Settings for the history trace layer (Phase B; no API key required).

    Attributes:
        text_cap_bytes: Maximum bytes of message ``text`` / event ``body``
            stored inline as a preview.  Larger payloads are truncated (the
            ``*_truncated`` flag is set) and remain reachable through
            ``membox history fetch``, which re-reads the upstream log via the
            row's ``payload_locator``.  Default ``16384`` per the lifecycle
            design (owner decision 2026-06-11: no Membox-managed blob storage).
    """

    text_cap_bytes: int = 16384


class IngestConfig(BaseModel):
    """Settings for synchronous chunk materialization.

    Attributes:
        concurrency: Maximum number of chunk extraction calls to run
            concurrently before serial SQLite materialization. Default 1 keeps
            the original single-threaded behaviour.
    """

    concurrency: int = Field(default_factory=lambda: int(_env("MEMBOX_INGEST_CONCURRENCY", "1")))


class MemboxConfig(BaseModel):
    """Top-level membox runtime configuration.

    Attributes:
        extraction: Provider settings for LLM triple extraction.
        embedding: Provider settings for text embedding.
        retrieval: Hybrid retrieval scoring and token-budget settings.
        history: History trace import/preview settings.
        ingest: Synchronous ingestion pipeline settings.
    """

    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
