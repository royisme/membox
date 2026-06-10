"""membox token estimation utilities.

Shared token estimation logic used by both :mod:`membox.core.store.retrieval`
and :mod:`membox.core.chunking` (the latter must not import from
``core.store`` to avoid a downward layering violation).

The estimator is a fast, deterministic approximation — it does not require
a real tokenizer and is suitable for guard-rail comparisons (e.g. deciding
whether a chunk is too large to send to an LLM with a fixed context window).
"""

from __future__ import annotations

import math
import re

# ---------------------------------------------------------------------------
# CJK character regex
# ---------------------------------------------------------------------------
# Characters that typically map to ~1 BPE token each.
_CJK_RE = re.compile(
    r"[一-鿿"
    r"㐀-䶿"
    r"\U00020000-\U0002a6df"
    r"　-〿"
    r"＀-￯"
    r"ぁ-ゟ"
    r"\u30a0-\u30ff"
    r"가-힯"
    r"]"
)


def est_tokens(s: str) -> int:
    """Deterministically estimate the token count of a string.

    Formula: ``CJK_count + ceil(non_CJK_count / 4)``.  CJK characters
    typically occupy one token each; non-CJK (ASCII-heavy) text is roughly
    4 characters per token on average.  Documented as an approximation;
    consistency matters more than accuracy.

    Args:
        s: Input string.

    Returns:
        Non-negative estimated token count.
    """
    cjk_count = sum(1 for _ in _CJK_RE.finditer(s))
    non_cjk_count = len(s) - cjk_count
    return cjk_count + math.ceil(non_cjk_count / 4)
