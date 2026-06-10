"""Tests for :mod:`membox.core.chunking` — markdown-aware section chunking."""

from __future__ import annotations

import pytest

from membox.core.chunking import chunk_markdown


class TestChunkMarkdownBasic:
    """Basic chunking behaviour on well-formed markdown."""

    def test_no_headings_returns_single_chunk(self) -> None:
        text = "Just a paragraph.\n\nAnd another."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        title, body = chunks[0]
        assert title is None
        assert "Just a paragraph." in body

    def test_empty_string_returns_single_empty_chunk(self) -> None:
        chunks = chunk_markdown("")
        assert len(chunks) == 1
        assert chunks[0] == (None, "")

    def test_single_section_no_preamble(self) -> None:
        text = "## Section A\nBody A"
        chunks = chunk_markdown(text)
        # No preamble content → one chunk only
        assert len(chunks) == 1
        assert chunks[0][0] == "Section A"
        assert chunks[0][1] == "Body A"

    def test_preamble_plus_section(self) -> None:
        text = "Preamble text.\n\n## Intro\nIntro body."
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0] == (None, "Preamble text.")
        assert chunks[1][0] == "Intro"
        assert chunks[1][1] == "Intro body."

    def test_multiple_sections(self) -> None:
        text = "## Alpha\nalpha body\n\n## Beta\nbeta body\n\n## Gamma\ngamma body"
        chunks = chunk_markdown(text)
        assert len(chunks) == 3
        assert chunks[0][0] == "Alpha"
        assert chunks[1][0] == "Beta"
        assert chunks[2][0] == "Gamma"

    def test_heading_text_stripped(self) -> None:
        text = "##   Spaced Title  \nBody."
        chunks = chunk_markdown(text)
        assert chunks[0][0] == "Spaced Title"

    def test_section_body_stripped(self) -> None:
        text = "## Title\n\n   Body with leading/trailing whitespace   \n\n"
        chunks = chunk_markdown(text)
        assert chunks[0][1] == "Body with leading/trailing whitespace"

    def test_h1_not_split_boundary(self) -> None:
        """Level-1 headings (# ...) must not create new chunks."""
        text = "# Top-level heading\nPreamble.\n\n## Section\nBody."
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0][0] is None
        assert "# Top-level heading" in chunks[0][1]

    def test_h3_not_split_boundary(self) -> None:
        """Level-3 headings (### ...) must not create new chunks."""
        text = "## Section A\nIntro.\n### Sub\nSub body."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == "Section A"
        assert "### Sub" in chunks[0][1]


class TestChunkMarkdownCodeFences:
    """Fenced code blocks must not be split even if they contain ## lines."""

    def test_backtick_fence_protects_heading(self) -> None:
        text = "## Outer\nSome code:\n```\n## fake heading\n```\nAfter fence."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == "Outer"
        assert "## fake heading" in chunks[0][1]

    def test_tilde_fence_protects_heading(self) -> None:
        text = "## Section\n~~~python\n## not a heading\n~~~\nDone."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == "Section"

    def test_long_fence_delimiter(self) -> None:
        """Four-backtick fences must also be handled."""
        text = "## A\n````\n## inside fence\n````\nafter"
        chunks = chunk_markdown(text)
        assert len(chunks) == 1

    def test_fence_mixed_with_real_sections(self) -> None:
        text = "## Real A\n```\n## not a section\n```\n## Real B\nbody B"
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0][0] == "Real A"
        assert chunks[1][0] == "Real B"

    def test_fence_must_close_with_same_char(self) -> None:
        """A backtick fence is NOT closed by a tilde line."""
        text = "## Sec\n```\n## inside\n~~~\n## still inside? no\n```\nafter"
        chunks = chunk_markdown(text)
        # After ``` opens, ~~~ does not close it, but the closing ``` does.
        assert len(chunks) == 1
        assert chunks[0][0] == "Sec"


class TestChunkMarkdownLineEndings:
    """CRLF and CR line endings must be normalised."""

    def test_crlf_normalised(self) -> None:
        text = "## Section\r\nBody line 1\r\nBody line 2"
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == "Section"
        assert "Body line 1" in chunks[0][1]

    def test_cr_normalised(self) -> None:
        text = "## CR Section\rBody"
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == "CR Section"

    def test_crlf_with_preamble(self) -> None:
        text = "Preamble\r\n\r\n## Heading\r\nBody"
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0] == (None, "Preamble")
        assert chunks[1][0] == "Heading"


class TestChunkMarkdownEdgeCases:
    """Edge cases and less common inputs."""

    def test_empty_section_body_skipped_by_caller_not_chunker(self) -> None:
        """The chunker returns all sections (including empty ones)."""
        text = "## A\n\n## B\nbody"
        chunks = chunk_markdown(text)
        # Section A has an empty body; chunker returns it — caller decides to skip
        titles = [t for t, _ in chunks]
        assert "A" in titles
        assert "B" in titles

    def test_bare_hash_hash_is_heading(self) -> None:
        """'##' alone (no trailing space) is still a valid heading."""
        text = "##\nbody"
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] == ""

    def test_only_preamble_no_sections(self) -> None:
        text = "# Title\n\nJust prose, no level-2 headings."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0][0] is None

    @pytest.mark.parametrize(
        "md",
        [
            "## A\n" * 50,
            "Preamble\n" + "\n".join(f"## Sec {i}\nBody {i}" for i in range(20)),
        ],
    )
    def test_many_sections(self, md: str) -> None:
        """Chunking never crashes on large documents."""
        chunks = chunk_markdown(md)
        assert len(chunks) > 0
