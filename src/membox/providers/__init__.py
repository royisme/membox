"""Provider adapter layer: protocol-level LLM clients (auth, request shape, error normalization).

Modules here adapt concrete provider wire protocols to the low-level
``ChatClient`` / ``EmbedClient`` Protocols. They contain no domain logic and
no prompts — those live in :mod:`membox.services`.
"""

from __future__ import annotations

from membox.providers.base import ChatClient, EmbedClient
from membox.providers.openai_compat import OpenAIChatClient, OpenAIEmbedClient

__all__ = [
    "ChatClient",
    "EmbedClient",
    "OpenAIChatClient",
    "OpenAIEmbedClient",
]
