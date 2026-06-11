"""membox triage — pure domain logic for the memory lifecycle gates.

Phase B ships only the secret-redaction scrubber; the heuristic triage gate
and its keyword tables arrive with Phase C.  Per the lifecycle design, the
pattern table is a small reviewed list living in code, redaction applies to
everything Membox stores (previews and FTS), and it is on by default and not
silently disablable per import.  No I/O happens in this module.
"""

from __future__ import annotations

import re

REDACTION_MARKER = "[REDACTED]"
"""Replacement text for every secret match."""

# Reviewed pattern list.  Order matters only for readability; all patterns are
# applied.  Each entry redacts the secret value, keeping surrounding context
# searchable (e.g. "OPENAI_API_KEY=[REDACTED]").
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Well-known token prefixes (OpenAI, Anthropic, GitHub, Slack, Stripe,
    # AWS access keys, Google API keys, JWTs).
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # KEY=/TOKEN=/SECRET=/PASSWORD= style assignments (env dumps, .env files).
    # The name part is kept; only the value is redacted.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)[A-Z0-9_]*"
        r"\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}\2"
    ),
    # PEM private-key blocks (multi-line).
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Bearer tokens in HTTP headers.
    re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
]


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings with :data:`REDACTION_MARKER`.

    Applied by the history store layer to message ``text`` and event ``body``
    before any persistence or FTS indexing, so secrets never become
    searchable.  ``history fetch`` re-reads the user's own upstream file and
    is intentionally outside this boundary.

    Args:
        text: Raw text from an imported session log.

    Returns:
        Text with every pattern match replaced.  Assignment-style matches
        keep the variable name and redact only the value.
    """
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(rf"\g<1>{REDACTION_MARKER}", text)
        else:
            text = pattern.sub(REDACTION_MARKER, text)
    return text
