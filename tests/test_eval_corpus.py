"""Tests that validate the Phase 7.5 M1 evaluation corpus and gold.yaml.

Checks:
- gold.yaml parses correctly via pyyaml.
- Every entry has required fields with correct types.
- Every source file referenced exists in eval/corpus/.
- Categories are from the allowed set.
- Per-category counts meet the Phase 7.5 M1 minimums.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

EVAL_DIR = pathlib.Path(__file__).parent.parent / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
GOLD_YAML = EVAL_DIR / "gold.yaml"

ALLOWED_CATEGORIES = {"single_hop", "multi_hop", "temporal"}
REQUIRED_FIELDS = {"id", "category", "question", "expected_keywords", "source"}

MIN_SINGLE_HOP = 12
MIN_MULTI_HOP = 6
MIN_TEMPORAL = 4


@pytest.fixture(scope="module")
def requires_corpus() -> None:
    """Skip tests that need eval/corpus/ if the directory is absent (e.g. in CI).

    eval/corpus/ contains private project handoff documents and is gitignored.
    The directory is only present on developer machines.
    """
    if not CORPUS_DIR.is_dir():
        pytest.skip(
            f"eval/corpus/ not found at {CORPUS_DIR} — local-only data (gitignored), skipped on CI"
        )


@pytest.fixture(scope="module")
def gold_entries() -> list[dict[str, Any]]:
    """Load and return all entries from gold.yaml.

    Returns:
        List of QA entry dicts parsed from gold.yaml.
    """
    assert GOLD_YAML.exists(), f"gold.yaml not found at {GOLD_YAML}"
    with GOLD_YAML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, list), "gold.yaml must be a YAML list at the top level"
    assert len(data) > 0, "gold.yaml must contain at least one entry"
    return data


def test_gold_yaml_parses(gold_entries: list[dict[str, Any]]) -> None:
    """gold.yaml must parse to a non-empty list."""
    assert len(gold_entries) >= 20, f"Expected at least 20 QA pairs, found {len(gold_entries)}"


def test_all_entries_have_required_fields(gold_entries: list[dict[str, Any]]) -> None:
    """Every entry must have all required fields with correct types.

    Args:
        gold_entries: Parsed list of QA dicts.
    """
    for entry in gold_entries:
        entry_id = entry.get("id", "<unknown>")
        missing = REQUIRED_FIELDS - set(entry.keys())
        assert not missing, f"Entry {entry_id} missing fields: {missing}"

        assert isinstance(entry["id"], str), f"Entry id must be str (got {type(entry['id'])})"
        assert isinstance(entry["category"], str), f"Entry {entry_id}: category must be str"
        assert isinstance(entry["question"], str), f"Entry {entry_id}: question must be str"
        assert isinstance(entry["expected_keywords"], list), (
            f"Entry {entry_id}: expected_keywords must be a list"
        )
        assert len(entry["expected_keywords"]) >= 2, (
            f"Entry {entry_id}: expected_keywords must have at least 2 items"
        )
        assert all(isinstance(k, str) for k in entry["expected_keywords"]), (
            f"Entry {entry_id}: all expected_keywords must be strings"
        )
        assert isinstance(entry["source"], list), f"Entry {entry_id}: source must be a list"
        assert len(entry["source"]) >= 1, f"Entry {entry_id}: source must have at least one file"
        assert all(isinstance(s, str) for s in entry["source"]), (
            f"Entry {entry_id}: all source entries must be strings"
        )


def test_all_categories_are_valid(gold_entries: list[dict[str, Any]]) -> None:
    """Every entry must use an allowed category.

    Args:
        gold_entries: Parsed list of QA dicts.
    """
    for entry in gold_entries:
        category = entry.get("category", "")
        assert category in ALLOWED_CATEGORIES, (
            f"Entry {entry.get('id')}: unknown category '{category}'. Allowed: {ALLOWED_CATEGORIES}"
        )


def test_source_files_exist(gold_entries: list[dict[str, Any]], requires_corpus: None) -> None:
    """Every file listed in source must exist in eval/corpus/.

    Args:
        gold_entries: Parsed list of QA dicts.
        requires_corpus: Fixture that skips if corpus dir is absent.
    """
    for entry in gold_entries:
        for filename in entry.get("source", []):
            corpus_file = CORPUS_DIR / filename
            assert corpus_file.exists(), (
                f"Entry {entry.get('id')}: source file '{filename}' not found in {CORPUS_DIR}"
            )


def test_category_count_minimums(gold_entries: list[dict[str, Any]]) -> None:
    """Per-category counts must meet Phase 7.5 M1 minimums.

    Args:
        gold_entries: Parsed list of QA dicts.
    """
    counts: dict[str, int] = {}
    for entry in gold_entries:
        cat = entry.get("category", "")
        counts[cat] = counts.get(cat, 0) + 1

    single_hop_count = counts.get("single_hop", 0)
    multi_hop_count = counts.get("multi_hop", 0)
    temporal_count = counts.get("temporal", 0)

    assert single_hop_count >= MIN_SINGLE_HOP, (
        f"single_hop count {single_hop_count} < minimum {MIN_SINGLE_HOP}"
    )
    assert multi_hop_count >= MIN_MULTI_HOP, (
        f"multi_hop count {multi_hop_count} < minimum {MIN_MULTI_HOP}"
    )
    assert temporal_count >= MIN_TEMPORAL, (
        f"temporal count {temporal_count} < minimum {MIN_TEMPORAL}"
    )


def test_ids_are_unique(gold_entries: list[dict[str, Any]]) -> None:
    """All entry IDs must be unique.

    Args:
        gold_entries: Parsed list of QA dicts.
    """
    ids = [entry.get("id") for entry in gold_entries]
    assert len(ids) == len(set(ids)), (
        f"Duplicate IDs found in gold.yaml: {[i for i in ids if ids.count(i) > 1]}"
    )


def test_corpus_dir_is_not_empty(requires_corpus: None) -> None:
    """eval/corpus/ must contain at least one .md file.

    Args:
        requires_corpus: Fixture that skips if corpus dir is absent.
    """
    md_files = list(CORPUS_DIR.glob("*.md"))
    assert len(md_files) >= 1, (
        f"eval/corpus/ has no .md files (found: {list(CORPUS_DIR.iterdir())})"
    )
