"""Unit tests for the membox triage gate (heuristic-v6 additions)."""

from __future__ import annotations

from membox.core.triage import _is_boilerplate_content, triage_trace


def test_is_boilerplate_bare_path_single_line() -> None:
    """A single bare file-path line with no prose is boilerplate."""
    assert _is_boilerplate_content("src/membox/cli/commands/ingest.py") is True


def test_is_boilerplate_bare_path_prose_not_caught() -> None:
    """A sentence that *contains* a path token is not boilerplate — prose has spaces."""
    assert _is_boilerplate_content("Edit src/foo/bar.py to fix the bug") is False


def test_triage_bare_path_is_boilerplate() -> None:
    """triage_trace drops a bare file-path message as boilerplate_content."""
    decision = triage_trace("src/membox/cli/commands/ingest.py", role="user")
    assert decision.should_extract is False
    assert decision.reason == "boilerplate_content"
