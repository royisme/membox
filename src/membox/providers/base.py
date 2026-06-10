"""Low-level provider client Protocols: ChatClient and EmbedClient.

These are wire-level primitives: a chat completion that returns text (with an
optional JSON output constraint) and a batch text-embedding call. Domain-level
interfaces (``LLMExtractor``, ``Embedder``) live in :mod:`membox.services` and
compose these primitives.

Design notes for future adapters (e.g. Gemini):

- The system prompt is a separate argument rather than part of a message
  list, because providers disagree on its transport (OpenAI: a ``system``
  role message; Gemini: a top-level ``system_instruction`` field).
- The JSON output constraint is expressed as a Pydantic model type; each
  adapter translates it to its native structured-output mechanism (OpenAI:
  ``response_format``; Gemini: ``response_schema`` + JSON mime type) and
  returns the raw JSON text for the caller to validate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pydantic import BaseModel


class ChatClient(Protocol):
    """Protocol for a single-turn chat completion against an LLM provider."""

    def complete(
        self,
        system: str,
        user: str,
        json_schema: type[BaseModel] | None = None,
    ) -> str:
        """Run one system+user completion and return the response text.

        Args:
            system: System prompt (provider adapters decide how to transport it).
            user: User message content.
            json_schema: Optional Pydantic model constraining the output to
                JSON matching the model's schema. When set, the returned
                string is JSON text (possibly empty on provider refusal);
                callers validate it themselves.

        Returns:
            Model response text; JSON text when ``json_schema`` is given.
        """
        ...


class EmbedClient(Protocol):
    """Protocol for batch text embedding against an embedding provider."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: Input strings to embed.

        Returns:
            One float vector per input text, in input order.
        """
        ...
