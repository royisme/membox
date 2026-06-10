"""OpenAI-compatible protocol adapter.

Implements the :class:`~membox.providers.base.ChatClient` and
:class:`~membox.providers.base.EmbedClient` Protocols on top of the ``openai``
SDK. Because the adapter only depends on the OpenAI wire protocol, pointing
the SDK client at a different ``base_url`` covers Ollama, vLLM, DeepSeek, and
other OpenAI-compatible servers without code changes.

This module owns authentication, request shaping, and response unwrapping
only — prompts and domain parsing live in :mod:`membox.services`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI
    from pydantic import BaseModel


class OpenAIChatClient:
    """ChatClient adapter over the OpenAI chat completions API.

    JSON-constrained calls use ``client.beta.chat.completions.parse`` so the
    server/SDK enforces the schema; the validated object is re-serialized to
    JSON text per the ``ChatClient`` contract.

    Attributes:
        max_completion_tokens: When set, passed as ``max_tokens`` to the
            completions API.  This caps completion length and is essential
            when targeting providers with small context windows (e.g. Ollama
            at 8 192 tokens) where leaving the cap at the server default can
            cause the JSON output to be truncated mid-generation.
    """

    def __init__(
        self,
        client: OpenAI,
        model: str,
        max_completion_tokens: int | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.max_completion_tokens = max_completion_tokens

    def complete(
        self,
        system: str,
        user: str,
        json_schema: type[BaseModel] | None = None,
    ) -> str:
        """Run one system+user chat completion.

        Args:
            system: System prompt, sent as a ``system`` role message.
            user: User message content.
            json_schema: Optional Pydantic model constraining output to JSON.

        Returns:
            Response text; JSON text when ``json_schema`` is given, or an
            empty string if the model refused to produce parseable output.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if json_schema is not None:
            kwargs: dict[str, object] = {
                "model": self.model,
                "messages": messages,
                "response_format": json_schema,
            }
            if self.max_completion_tokens is not None:
                # ``max_tokens`` is the parameter name accepted by both the
                # standard OpenAI API and OpenAI-compatible servers (Ollama,
                # vLLM, DeepSeek).  The newer ``max_completion_tokens`` alias
                # is only recognised by recent OpenAI API versions and is
                # rejected by most local-server implementations.
                kwargs["max_tokens"] = self.max_completion_tokens
            rsp = self.client.beta.chat.completions.parse(
                **kwargs,  # type: ignore[arg-type]
            )
            parsed = rsp.choices[0].message.parsed
            if parsed is None:
                return ""
            return parsed.model_dump_json()
        plain_kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
        }
        if self.max_completion_tokens is not None:
            plain_kwargs["max_tokens"] = self.max_completion_tokens
        plain = self.client.chat.completions.create(  # type: ignore[call-overload]
            **plain_kwargs,
        )
        return plain.choices[0].message.content or ""


class OpenAIEmbedClient:
    """EmbedClient adapter over the OpenAI embeddings API."""

    def __init__(self, client: OpenAI, model: str, dimensions: int) -> None:
        self.client = client
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via the OpenAI embeddings API.

        Single-item batches are sent as a scalar ``input`` (both forms are
        accepted by the wire protocol; the scalar form preserves the request
        shape membox has always emitted).

        Args:
            texts: Input strings to embed.

        Returns:
            One float vector per input text, in input order.
        """
        if not texts:
            return []
        payload: str | list[str] = texts[0] if len(texts) == 1 else texts
        rsp = self.client.embeddings.create(
            model=self.model,
            input=payload,
            dimensions=self.dimensions,
        )
        return [list(item.embedding) for item in rsp.data]
