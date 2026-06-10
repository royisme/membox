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
- When *max_tokens* is given (positive integer), any chunk whose estimated
  token count exceeds the limit is further split on blank-line paragraph
  boundaries.  Fenced code blocks are never split mid-block.  Each
  sub-chunk is labelled ``"<title> (N/M)"`` where N and M are 1-based
  position and total count; preamble sub-chunks use ``None`` as the base
  title so the label becomes ``"(N/M)"``.

This module is pure domain logic — it performs no I/O.
"""

from __future__ import annotations

import re

from membox.core.tokens import est_tokens

# Heading pattern: a line that starts with exactly "##" (two hashes), optionally
# followed by whitespace and the heading text.  Matches "##" alone too.
_HEADING_RE = re.compile(r"^#{2}(?:\s+(.*)|\s*$)")

# Fence delimiter lines: ``` or ~~~ (optionally followed by a language tag).
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# Default maximum estimated tokens per chunk.  Sections that exceed this limit
# are split further on paragraph boundaries so that LLMs with limited context
# windows (e.g. Ollama defaults at 8 192 tokens) can process every chunk.
_DEFAULT_MAX_TOKENS: int = 2000


def chunk_markdown(
    text: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> list[tuple[str | None, str]]:
    """Split a markdown document into section chunks on ``##`` headings.

    Each chunk is a ``(section_title, content)`` pair where ``section_title``
    is the heading text (stripped, without the leading ``##``) and ``content``
    is the body of that section.  The preamble — text before the first ``##``
    heading — is returned with ``section_title=None`` only when it has
    non-whitespace content.

    When *max_tokens* is positive, any chunk whose
    :func:`~membox.core.tokens.est_tokens` count exceeds the limit is further
    split on paragraph boundaries (blank lines).  Fenced code blocks are never
    broken mid-block.  Sub-chunks are labelled ``"<title> (N/M)"``; the first
    sub-chunk is ``"<title> (1/M)"``.  When there is only one part (no
    splitting needed) the title is returned unchanged.

    Args:
        text: Raw markdown string.  CRLF line endings are handled automatically.
        max_tokens: Maximum estimated tokens allowed per chunk.  Chunks that
            exceed this limit are split on paragraph boundaries.  Pass ``0``
            or a negative value to disable sub-chunking entirely.

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

    raw_chunks: list[tuple[str | None, str]] = []
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
                raw_chunks.append((current_title, body))
            elif body:
                # Preamble — only emit if there is actual content.
                raw_chunks.append((current_title, body))
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
        raw_chunks.append((current_title, body))
    elif body:
        # No headings at all AND content exists — whole doc is one preamble chunk.
        raw_chunks.append((current_title, body))
    elif not raw_chunks:
        # Empty input or only whitespace: return a single empty preamble chunk.
        raw_chunks.append((None, ""))

    # Sub-chunking pass: split oversized chunks on paragraph boundaries.
    if max_tokens > 0:
        return _apply_max_tokens(raw_chunks, max_tokens)
    return raw_chunks


# ---------------------------------------------------------------------------
# Sub-chunking helpers
# ---------------------------------------------------------------------------


def _split_paragraphs(body: str) -> list[str]:
    """Split *body* into paragraphs on blank lines, never inside fenced blocks.

    A paragraph boundary is one or more consecutive blank lines (lines with
    no non-whitespace content).  Fenced code blocks (``` or ~~~) are treated
    as atomic units and are never split.

    Args:
        body: Section body text (already stripped).

    Returns:
        Non-empty paragraph strings in document order.
    """
    lines = body.split("\n")
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False
    fence_char = ""

    for line in lines:
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            opener = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = opener[0]
            elif opener[0] == fence_char:
                in_fence = False
                fence_char = ""
            current.append(line)
            continue

        if in_fence:
            current.append(line)
            continue

        if line.strip() == "":
            # Blank line — paragraph boundary (only when outside a fence).
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)

    if current:
        paragraphs.append("\n".join(current))

    return [p for p in paragraphs if p.strip()]


def _apply_max_tokens(
    raw_chunks: list[tuple[str | None, str]],
    max_tokens: int,
) -> list[tuple[str | None, str]]:
    """Apply paragraph-level sub-chunking to chunks that exceed *max_tokens*.

    Chunks within the limit are returned unchanged.  Oversized chunks are
    split into paragraph groups; each group is packed greedily until adding
    the next paragraph would exceed the limit.  Sub-chunk titles use the
    ``"<title> (N/M)"`` format.

    Args:
        raw_chunks: Section-level chunks from the first pass.
        max_tokens: Per-chunk token ceiling (positive).

    Returns:
        Potentially expanded list of ``(title, content)`` tuples.
    """
    result: list[tuple[str | None, str]] = []
    for title, body in raw_chunks:
        if est_tokens(body) <= max_tokens:
            result.append((title, body))
            continue

        # Split body into paragraphs and re-pack into groups.
        paragraphs = _split_paragraphs(body)
        if not paragraphs:
            result.append((title, body))
            continue

        groups: list[str] = []
        current_paras: list[str] = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = est_tokens(para)
            if current_paras and current_tokens + para_tokens > max_tokens:
                # Flush current group.
                groups.append("\n\n".join(current_paras))
                current_paras = [para]
                current_tokens = para_tokens
            else:
                current_paras.append(para)
                current_tokens += para_tokens

        if current_paras:
            groups.append("\n\n".join(current_paras))

        if len(groups) == 1:
            # Whole body fits as one group (all paragraphs individually under
            # limit but total over limit — rare edge case: just emit as-is).
            result.append((title, body))
        else:
            total = len(groups)
            for idx, group_body in enumerate(groups, start=1):
                if title is not None:
                    sub_title: str | None = f"{title} ({idx}/{total})"
                else:
                    sub_title = f"({idx}/{total})"
                result.append((sub_title, group_body))

    return result
