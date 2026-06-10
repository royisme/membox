"""Tests for extraction-length-limit fixes (fix/extraction-length-limits).

Covers:
1. Sub-chunk splitting (core/chunking.py + core/tokens.py):
   - Over-limit sections are split on paragraph boundaries.
   - Fenced code blocks are never split mid-block.
   - Sub-chunk title naming convention ("Section (N/M)").
   - Chunks within the limit are returned unchanged.
   - Preamble sub-chunks use "(N/M)" as title (no leading title word).

2. Resilient ingestion (core/agent.py):
   - A failing extractor for one chunk does not abort ingest_file.
   - The failure is recorded in the result list with "section" and "error".
   - Successful chunks before and after the failure are ingested.
   - KeyboardInterrupt is re-raised immediately and never swallowed.

3. Provider token cap (providers/openai_compat.py):
   - OpenAIChatClient passes max_tokens to the completions API when
     max_completion_tokens is set.
   - Calls with json_schema also receive max_tokens.
   - max_tokens is omitted when max_completion_tokens is None (default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from membox.core.agent import MemoryAgent
from membox.core.chunking import _DEFAULT_MAX_TOKENS, chunk_markdown
from membox.model.schema import ExtractedGraph
from membox.providers.openai_compat import OpenAIChatClient

# ---------------------------------------------------------------------------
# Helper: build a body whose token estimate is roughly n_tokens
# ---------------------------------------------------------------------------


def _ascii_body(approx_tokens: int) -> str:
    """Return an ASCII string whose est_tokens count is ≥ approx_tokens.

    Uses pure-ASCII words so each ~4 chars → 1 token, giving predictable
    estimates without CJK characters.
    """
    word = "word "
    # Each "word " = 5 chars ≈ 1.25 tokens.  Over-produce, then trim.
    repetitions = (approx_tokens * 4 // len(word)) + 10
    return (word * repetitions).strip()


# ===========================================================================
# 1. Sub-chunk splitting
# ===========================================================================


class TestSubChunkSplitting:
    """chunk_markdown sub-chunking on over-limit sections."""

    def test_small_section_not_split(self) -> None:
        """A section within the token limit is returned as-is."""
        text = "## Section\nShort body."
        chunks = chunk_markdown(text, max_tokens=2000)
        assert len(chunks) == 1
        assert chunks[0][0] == "Section"
        assert chunks[0][1] == "Short body."

    def test_oversized_section_splits_into_multiple_chunks(self) -> None:
        """A section exceeding max_tokens is split on blank-line boundaries."""
        # Build three paragraphs each ≈ 600 tokens so combined > 1500 (limit).
        para1 = _ascii_body(600)
        para2 = _ascii_body(600)
        para3 = _ascii_body(600)
        body = f"{para1}\n\n{para2}\n\n{para3}"
        text = f"## Big Section\n{body}"
        chunks = chunk_markdown(text, max_tokens=1500)
        # Must have more than 1 chunk.
        assert len(chunks) > 1
        # All titles should follow "Big Section (N/M)" pattern.
        for title, _ in chunks:
            assert title is not None
            assert "Big Section" in title
            assert "/" in title

    def test_sub_chunk_titles_numbered_correctly(self) -> None:
        """Sub-chunk titles follow the (N/M) format in document order."""
        para = _ascii_body(700)
        body = "\n\n".join([para] * 3)
        text = f"## MySection\n{body}"
        chunks = chunk_markdown(text, max_tokens=1400)
        titles = [t for t, _ in chunks]
        total = len(titles)
        assert total > 1
        for idx, title in enumerate(titles, start=1):
            assert title == f"MySection ({idx}/{total})"

    def test_preamble_sub_chunks_title_format(self) -> None:
        """Over-limit preamble sub-chunks use '(N/M)' as title, not 'None (N/M)'."""
        para = _ascii_body(700)
        body = "\n\n".join([para] * 3)
        # Preamble: no ## heading before content.
        chunks = chunk_markdown(body, max_tokens=1400)
        assert len(chunks) > 1
        for _idx, (title, _) in enumerate(chunks, start=1):
            # Title should be "(N/M)", never "None (N/M)".
            assert title is not None
            assert "None" not in str(title)
            assert title.startswith("(")

    def test_fence_not_split_across_paragraph_boundary(self) -> None:
        """A fenced code block is never broken across sub-chunk boundaries."""
        # Build a block whose total size is over the limit; code fence is
        # one of the "paragraphs" — it must stay atomic.
        preamble = _ascii_body(700)
        fence_block = "```python\n" + ("x = 1\n" * 100) + "```"
        trailer = _ascii_body(700)
        body = f"{preamble}\n\n{fence_block}\n\n{trailer}"
        text = f"## Code Section\n{body}"
        chunks = chunk_markdown(text, max_tokens=1400)
        # Verify no chunk ends mid-fence (fence lines must all be in the same chunk).
        fence_lines_found = False
        for _, chunk_body in chunks:
            lines = chunk_body.split("\n")
            fence_opens = sum(1 for ln in lines if ln.startswith("```"))
            # In any single chunk, fence opens/closes must be balanced (even count).
            assert fence_opens % 2 == 0, (
                f"Unbalanced fences in chunk (opens={fence_opens}): {chunk_body[:120]!r}"
            )
            if "x = 1" in chunk_body:
                fence_lines_found = True
        assert fence_lines_found, "Fence block content not found in any chunk"

    def test_max_tokens_zero_disables_subchunking(self) -> None:
        """Passing max_tokens=0 disables sub-chunking entirely."""
        para = _ascii_body(700)
        body = "\n\n".join([para] * 4)
        text = f"## Sec\n{body}"
        chunks = chunk_markdown(text, max_tokens=0)
        assert len(chunks) == 1
        assert chunks[0][0] == "Sec"

    def test_default_max_tokens_constant_reasonable(self) -> None:
        """_DEFAULT_MAX_TOKENS should be a positive integer (sanity check)."""
        assert isinstance(_DEFAULT_MAX_TOKENS, int)
        assert _DEFAULT_MAX_TOKENS > 0

    def test_existing_small_document_unchanged(self) -> None:
        """Real-world small documents produce identical output to old behaviour."""
        text = (
            "## Summary\nThis document summarises the project.\n\n## Details\nSome details here.\n"
        )
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0] == ("Summary", "This document summarises the project.")
        assert chunks[1] == ("Details", "Some details here.")


# ===========================================================================
# 2. Resilient ingestion
# ===========================================================================


class _FailingExtractor:
    """Test double: raises on extract() for every call."""

    def extract(self, text: str) -> ExtractedGraph:
        """Always fail.

        Args:
            text: Ignored.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        msg = "simulated extraction failure"
        raise RuntimeError(msg)

    def extract_query_entities(self, text: str) -> list[str]:
        """Return empty list (query path not exercised here).

        Args:
            text: Ignored.

        Returns:
            Empty list.
        """
        return []


class _PartialFailExtractor:
    """Test double: fails only on a specific call number, succeeds otherwise."""

    def __init__(self, fail_on: int = 2) -> None:
        self._calls = 0
        self._fail_on = fail_on

    def extract(self, text: str) -> ExtractedGraph:
        """Succeed on all calls except the *fail_on*-th call.

        Args:
            text: Ignored.

        Returns:
            Empty ExtractedGraph for all but the *fail_on*-th call.

        Raises:
            RuntimeError: On the *fail_on*-th call.
        """
        self._calls += 1
        if self._calls == self._fail_on:
            msg = f"failure on call {self._calls}"
            raise RuntimeError(msg)
        return ExtractedGraph(entities=[], relations=[])

    def extract_query_entities(self, text: str) -> list[str]:
        """Return empty list.

        Args:
            text: Ignored.

        Returns:
            Empty list.
        """
        return []


class TestResilientIngestion:
    """MemoryAgent.ingest_file continues past per-chunk extraction failures."""

    def _md_file(self, tmp_path: Path, n_sections: int = 3) -> Path:
        sections = "\n\n".join(
            f"## Section {i}\nContent for section {i}." for i in range(1, n_sections + 1)
        )
        md = tmp_path / "doc.md"
        md.write_text(sections, encoding="utf-8")
        return md

    def test_all_fail_returns_error_entries(self, tmp_path: Path) -> None:
        """When all chunks fail, result list contains error entries for each."""
        md = self._md_file(tmp_path)
        agent = MemoryAgent(extractor=_FailingExtractor(), db_path=str(tmp_path / "a.db"))
        results = agent.ingest_file(md)
        assert len(results) == 3
        for r in results:
            assert "error" in r
            assert "section" in r
            assert "simulated extraction failure" in str(r["error"])

    def test_partial_failure_continues_to_next_chunk(self, tmp_path: Path) -> None:
        """Failure on one chunk does not abort subsequent chunks."""
        # Call 1 succeeds, call 2 fails, call 3 succeeds.
        md = self._md_file(tmp_path, n_sections=3)
        extractor = _PartialFailExtractor(fail_on=2)
        agent = MemoryAgent(extractor=extractor, db_path=str(tmp_path / "a.db"))
        results = agent.ingest_file(md)
        assert len(results) == 3
        # First result should be a success (has doc_id).
        assert "doc_id" in results[0]
        # Second result should be a failure.
        assert "error" in results[1]
        # Third result should be a success again.
        assert "doc_id" in results[2]

    def test_success_chunks_actually_stored(self, tmp_path: Path) -> None:
        """Chunks that succeed are stored even when others in the same file fail."""
        md = self._md_file(tmp_path, n_sections=2)
        extractor = _PartialFailExtractor(fail_on=2)  # first OK, second fails
        agent = MemoryAgent(extractor=extractor, db_path=str(tmp_path / "a.db"))
        results = agent.ingest_file(md)
        ok_results = [r for r in results if "doc_id" in r]
        assert len(ok_results) == 1
        # Verify the document was actually written to the store.
        count = agent.store._conn().execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
        assert count == 1

    def test_keyboard_interrupt_propagates(self, tmp_path: Path) -> None:
        """KeyboardInterrupt is never caught by ingest_file."""

        class _KbdExtractor:
            def extract(self, text: str) -> ExtractedGraph:
                raise KeyboardInterrupt

            def extract_query_entities(self, text: str) -> list[str]:
                return []

        md = self._md_file(tmp_path, n_sections=1)
        agent = MemoryAgent(extractor=_KbdExtractor(), db_path=str(tmp_path / "a.db"))
        with pytest.raises(KeyboardInterrupt):
            agent.ingest_file(md)

    def test_error_entry_has_section_key(self, tmp_path: Path) -> None:
        """Failed result entries always include the 'section' key."""
        md = self._md_file(tmp_path, n_sections=1)
        agent = MemoryAgent(extractor=_FailingExtractor(), db_path=str(tmp_path / "a.db"))
        results = agent.ingest_file(md)
        assert len(results) == 1
        assert "section" in results[0]
        assert results[0]["section"] == "Section 1"


# ===========================================================================
# 3. Provider token cap
# ===========================================================================


class TestProviderTokenCap:
    """OpenAIChatClient passes max_tokens when max_completion_tokens is set."""

    def _make_client(
        self, max_completion_tokens: int | None = None
    ) -> tuple[Any, OpenAIChatClient]:
        """Return (fake_openai_client, OpenAIChatClient) pair."""

        fake_openai = MagicMock()
        # Wire up the beta.chat.completions.parse mock response.
        parsed_mock = MagicMock()
        parsed_mock.model_dump_json.return_value = '{"entities":[],"relations":[]}'
        fake_openai.beta.chat.completions.parse.return_value.choices[0].message.parsed = parsed_mock
        # Wire up the plain chat.completions.create mock response.
        fake_openai.chat.completions.create.return_value.choices[
            0
        ].message.content = "plain response"

        chat_client = OpenAIChatClient(
            fake_openai,
            "test-model",
            max_completion_tokens=max_completion_tokens,
        )
        return fake_openai, chat_client

    def test_max_tokens_passed_in_json_schema_call(self) -> None:
        """max_tokens is forwarded when calling with json_schema."""
        from pydantic import BaseModel

        class _Schema(BaseModel):
            entities: list[str] = []
            relations: list[str] = []

        fake_openai, chat_client = self._make_client(max_completion_tokens=2048)
        chat_client.complete("sys", "user", json_schema=_Schema)

        call_kwargs = fake_openai.beta.chat.completions.parse.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert kwargs.get("max_tokens") == 2048

    def test_max_tokens_not_passed_when_none(self) -> None:
        """When max_completion_tokens is None, max_tokens is not forwarded."""
        from pydantic import BaseModel

        class _Schema(BaseModel):
            entities: list[str] = []
            relations: list[str] = []

        fake_openai, chat_client = self._make_client(max_completion_tokens=None)
        chat_client.complete("sys", "user", json_schema=_Schema)

        call_kwargs = fake_openai.beta.chat.completions.parse.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert "max_tokens" not in kwargs

    def test_max_tokens_passed_in_plain_call(self) -> None:
        """max_tokens is forwarded on plain (no json_schema) completions too."""
        fake_openai, chat_client = self._make_client(max_completion_tokens=1024)
        chat_client.complete("sys", "user")

        call_kwargs = fake_openai.chat.completions.create.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert kwargs.get("max_tokens") == 1024

    def test_max_tokens_not_passed_in_plain_call_when_none(self) -> None:
        """max_tokens absent in plain call when max_completion_tokens=None."""
        fake_openai, chat_client = self._make_client(max_completion_tokens=None)
        chat_client.complete("sys", "user")

        call_kwargs = fake_openai.chat.completions.create.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert "max_tokens" not in kwargs

    def test_default_max_completion_tokens_is_none(self) -> None:
        """OpenAIChatClient default leaves max_completion_tokens=None."""
        from membox.providers.openai_compat import OpenAIChatClient

        client = OpenAIChatClient(MagicMock(), "model")
        assert client.max_completion_tokens is None
