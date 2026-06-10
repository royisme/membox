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
        (``OPENAI_API_KEY`` for unknown providers).

        Returns:
            API key string, or None if neither config nor environment has one.
        """
        if self.api_key is not None:
            return self.api_key
        env_var = _PROVIDER_ENV_VARS.get(self.provider, _DEFAULT_ENV_VAR)
        return os.environ.get(env_var)


class ExtractionConfig(ProviderConfig):
    """Provider settings for the LLM extraction capability."""

    model: str = "gpt-4o-mini"


class EmbeddingConfig(ProviderConfig):
    """Provider settings for the embedding capability.

    Attributes:
        dimensions: Embedding vector dimensionality requested from the API.
    """

    model: str = "text-embedding-3-small"
    dimensions: int = 1536


class MemboxConfig(BaseModel):
    """Top-level membox runtime configuration.

    Attributes:
        extraction: Provider settings for LLM triple extraction.
        embedding: Provider settings for text embedding.
    """

    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
