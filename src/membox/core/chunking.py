"""Markdown-aware document chunking for membox ingestion.

Splits a markdown document into (section_title, content) pairs on ``##``
heading boundaries.  Content that appears before the first ``##`` heading is
returned as a preamble chunk with the section title ``None``.

Rules:
- Only ``##`` headings (level-2) are used as split boundaries; level-1 (``#``)
  and deeper headings (``###``, ``####`` …) are left intact inside chunks.
- Lines inside fenced code blocks (delimited by triple back-ticks or ``~~~``)
  are never treated as headings, even if they match the ``## …`` pattern.
- CRLF line endings are normalised to LF before processing.
- A document with no ``##`` headings is returned as a single chunk whose
  section title is ``None``.
- An empty preamble (text before the first ``##`` has no non-whitespace
  content) is NOT emitted as a separate chunk; only preambles with actual
  content are returned.
- Named sections (with a title) are always returned, even when their body
  is empty, so callers can decide whether to skip them.

This module is pure domain logic — it performs no I/O.
"""

from __future__ import annotations

import re

# Heading pattern: a line that starts with exactly "##" (two hashes), optionally
# followed by whitespace and the heading text.  Matches "##" alone too.
_HEADING_RE = re.compile(r"^#{2}(?:\s+(.*)|\s*$)")

# Fence delimiter lines: ``` or ~~~ (optionally followed by a language tag).
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """Split a markdown document into section chunks on ``##`` headings.

    Each chunk is a ``(section_title, content)`` pair where ``section_title``
    is the heading text (stripped, without the leading ``##``) and ``content``
    is the body of that section.  The preamble — text before the first ``##``
    heading — is returned with ``section_title=None`` only when it has
    non-whitespace content.

    Args:
        text: Raw markdown string.  CRLF line endings are handled automatically.

    Returns:
        List of ``(section_title, content)`` tuples in document order.  The
        list always has at least one element; an empty string input returns
        ``[(None, "")]``.

    Examples:
        >>> chunks = chunk_markdown("preamble\\n\\n## Section A\\nbody A")
        >>> chunks[0]
        (None, 'preamble')
        >>> chunks[1]
        ('Section A', 'body A')

        >>> chunks = chunk_markdown("## Only Section\\nbody")
        >>> len(chunks)
        1
        >>> chunks[0]
        ('Only Section', 'body')
    """
    # Normalise line endings.
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")

    chunks: list[tuple[str | None, str]] = []
    # Start in preamble mode: current_title is None until the first ## heading.
    current_title: str | None = None
    current_lines: list[str] = []
    in_fence = False
    fence_char: str = ""
    # Track whether we have seen any ## heading yet (to distinguish preamble
    # from a named section).
    seen_heading = False

    for line in lines:
        # Track fenced code block state.
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            opener = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = opener[0]  # '`' or '~'
            elif opener[0] == fence_char:
                # Closing fence must use the same character as the opener.
                in_fence = False
                fence_char = ""
            current_lines.append(line)
            continue

        # Inside a fenced block: never treat as a heading.
        if in_fence:
            current_lines.append(line)
            continue

        # Check for a level-2 heading outside a fence.
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            # Flush accumulated content as a completed chunk.
            body = "\n".join(current_lines).strip()
            if seen_heading:
                # Named section — always emit, even when body is empty.
                chunks.append((current_title, body))
            elif body:
                # Preamble — only emit if there is actual content.
                chunks.append((current_title, body))
            # else: empty preamble before first heading → silently discard.

            # Capture heading title: group 1 is the text after "## " or None.
            title_group = heading_match.group(1)
            current_title = title_group.strip() if title_group is not None else ""
            current_lines = []
            seen_heading = True
        else:
            current_lines.append(line)

    # Flush the final accumulated content.
    body = "\n".join(current_lines).strip()
    if seen_heading:
        # Named section — always emit.
        chunks.append((current_title, body))
    elif body:
        # No headings at all AND content exists — whole doc is one preamble chunk.
        chunks.append((current_title, body))
    elif not chunks:
        # Empty input or only whitespace: return a single empty preamble chunk.
        chunks.append((None, ""))

    return chunks
