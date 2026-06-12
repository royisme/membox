"""Tests for committed Phase C lifecycle eval fixtures.

These tests intentionally do not depend on migration 8. They prove the
synthetic trace corpus is importable through the existing history layer and
that the expectation file is strict enough for later triage/extraction work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from membox.core.history_import import import_history
from membox.core.store import KnowledgeStore

ROOT = Path(__file__).parent.parent
LIFECYCLE_DIR = ROOT / "eval" / "lifecycle"
HISTORY_DIR = LIFECYCLE_DIR / "history"
EXPECTATIONS = LIFECYCLE_DIR / "expectations.yaml"
COMPARATOR_CASES = LIFECYCLE_DIR / "comparator_cases.yaml"

REQUIRED_CATEGORIES = {
    "explicit_user_rules",
    "ephemeral_chatter",
    "plan_to_decision",
    "superseded_facts",
    "repeated_failures",
    "conflicting_memories",
    "user_corrections",
    # D0 real-trace false-positive families (anonymized reconstructions).
    "tool_output_noise",
    "harness_template_noise",
}
MULTI_SESSION_CATEGORIES = {
    "plan_to_decision",
    "superseded_facts",
    "conflicting_memories",
    "user_corrections",
    "harness_template_noise",
}
VALID_UNIT_TYPES = {
    "preference",
    "decision",
    "procedure",
    "fact",
    "learning",
    "plan",
    "event",
    "context",
}
VALID_LABELS = {
    "architecture",
    "storage",
    "retrieval",
    "cli",
    "testing",
    "tooling",
    "workflow",
    "conventions",
    "dependencies",
    "performance",
    "security",
}
VALID_ACTIVATION_STATUSES = {"trace_only", "unit_candidate", "active_unit"}

# Phase-D vocabulary recorded at C1 time.  These are closed sets derived from
# the values present in eval/lifecycle/expectations.yaml across all 7 entries
# (including c7).  Extend here when new Phase-D scenarios are added.
VALID_PHASE_D_STATUSES: frozenset[str] = frozenset(
    {
        "conflict_review",
        "crystal_candidate",
        "not_applicable",
        "retracted_or_superseded",
        "superseded",
        "supersedes_plan",
    }
)
VALID_QUERY_INCLUSIONS: frozenset[str] = frozenset(
    {
        "corrected_only",
        "excluded",
        "explicit_memory_only",
        "latest_only",
        "surface_conflict",
    }
)


def _load_expectations() -> list[dict[str, Any]]:
    """Load lifecycle expectations from YAML."""
    data = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return cast("list[dict[str, Any]]", data)


def _contains_cjk(text: str) -> bool:
    """Return True when text contains a CJK unified ideograph."""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def test_lifecycle_expectations_cover_required_categories() -> None:
    """The committed corpus must cover every Phase C scenario category."""
    entries = _load_expectations()
    categories = {str(entry["category"]) for entry in entries}
    assert categories == REQUIRED_CATEGORIES
    assert {str(entry["id"]) for entry in entries} >= {
        "c5_repeated_failure_learning",
        "c5_repeated_failure_learning_b",
    }


def test_lifecycle_expectation_schema_is_strict() -> None:
    """Every expectation declares triage, extraction, source, and query outcomes."""
    for entry in _load_expectations():
        assert isinstance(entry["id"], str)
        assert isinstance(entry["fixtures"], list)
        assert all(isinstance(item, str) for item in entry["fixtures"])
        assert isinstance(entry["requires_multi_session"], bool)
        expected = entry["expected"]
        assert isinstance(expected, dict)

        assert isinstance(expected["triage_should_extract"], bool)
        assert expected["unit_type"] in VALID_UNIT_TYPES
        assert expected["activation_status"] in VALID_ACTIVATION_STATUSES
        assert expected["phase_d_status"] in VALID_PHASE_D_STATUSES
        assert expected["query_inclusion"] in VALID_QUERY_INCLUSIONS
        assert expected["distill"] in {"none", "candidate_with_c5"}
        assert isinstance(expected["extraction_hint"], str)

        labels = expected["labels"]
        assert isinstance(labels, list)
        assert set(labels).issubset(VALID_LABELS)

        source_refs = expected["source_refs"]
        assert isinstance(source_refs, list)
        assert source_refs
        for source_ref in source_refs:
            assert source_ref["trace_kind"] in {"message", "event"}
            assert isinstance(source_ref["trace_id"], str)


def test_lifecycle_multi_session_scenarios_use_distinct_sessions() -> None:
    """Scenarios 3, 4, 6, and 7 must be backed by distinct sessions."""
    for entry in _load_expectations():
        category = str(entry["category"])
        fixtures = [str(item) for item in entry["fixtures"]]
        if category in MULTI_SESSION_CATEGORIES:
            assert entry["requires_multi_session"] is True
            assert len(fixtures) >= 2
        else:
            assert entry["requires_multi_session"] is False


def test_lifecycle_history_fixtures_are_importable(tmp_path: Path) -> None:
    """All referenced JSONL fixtures import through the history layer."""
    store = KnowledgeStore(str(tmp_path / "lifecycle.db"))
    imported: set[str] = set()
    for entry in _load_expectations():
        for fixture_name in entry["fixtures"]:
            fixture = LIFECYCLE_DIR / str(fixture_name)
            assert fixture.is_file()
            if str(fixture) not in imported:
                result = import_history(
                    store,
                    fixture,
                    "membox",
                    project="membox-lifecycle",
                )
                assert result["skipped"] is False
                imported.add(str(fixture))

        expected = entry["expected"]
        for source_ref in expected["source_refs"]:
            row = store.get_history_record(str(source_ref["trace_id"]))
            assert row is not None
            assert row["project"] == "membox-lifecycle"


def test_lifecycle_fixtures_include_cjk_cases() -> None:
    """At least two lifecycle fixture files contain CJK text."""
    cjk_files = [
        path
        for path in sorted(HISTORY_DIR.glob("*.jsonl"))
        if _contains_cjk(path.read_text(encoding="utf-8"))
    ]
    assert len(cjk_files) >= 2


def test_comparator_cases_schema_and_agreement_gate() -> None:
    """The committed comparator corpus is labeled and passes the offline gate."""
    data = yaml.safe_load(COMPARATOR_CASES.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["score_threshold"] == 0.5
    assert data["min_agreement"] >= 0.8
    cases = data["cases"]
    assert isinstance(cases, list)
    assert len(cases) >= 5
    assert {case["human_label"] for case in cases} == {"keep", "drop"}
    assert all(0.0 <= float(case["llm_score"]) <= 1.0 for case in cases)

    import importlib.util

    script_path = Path(__file__).parent.parent / "scripts" / "eval_lifecycle_comparator.py"
    spec = importlib.util.spec_from_file_location("eval_lifecycle_comparator", script_path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    assert mod.main([]) == 0
