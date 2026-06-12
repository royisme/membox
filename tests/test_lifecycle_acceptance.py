"""C5 acceptance harness for the Phase C heuristic triage gate.

Runs the C1 fixture corpus end-to-end and records four metrics:

- **triage precision** - rejected chatter must not become units (no false positives)
- **triage recall**   - explicit rules must be selected (no false negatives)
- **type accuracy**   - predicted unit_type matches gold expectation
- **duplicate rate**  - units after second apply equals units after first apply

The harness asserts exact agreement (all metrics == 1.0 / 0.0) for metrics that
currently pass.  Where the gate disagrees with the gold, the assertion is left as
a documented ``xfail`` with an explanation; the gate and ``expectations.yaml`` are
not modified.

Multi-source entries (c3, c4, c5, c6, c7): the harness treats each source ref
independently.  For *precision/recall*, an entry is considered ``should_extract``
if **any** of its source refs passes the gate (consistent with how the real CLI
pipeline works — any matching trace produces a unit candidate).  For *type
accuracy*, we check the source ref that passes; if none does, the entry is
excluded from type accuracy (already counted as a recall miss).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

from membox.cli import app
from membox.core.history_import import import_history
from membox.core.store import KnowledgeStore
from membox.core.triage import GATE_VERSION, activation_passes, triage_trace
from membox.model.schema import MemoryUnitType

ROOT = Path(__file__).parent.parent
LIFECYCLE_DIR = ROOT / "eval" / "lifecycle"
EXPECTATIONS = LIFECYCLE_DIR / "expectations.yaml"

_PROJECT = "membox-lifecycle"


def _load_expectations() -> list[dict[str, Any]]:
    """Load lifecycle expectations from YAML."""
    data = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return cast("list[dict[str, Any]]", data)


def _build_store(tmp_path: Path) -> KnowledgeStore:
    """Import all lifecycle fixtures into a fresh store and return it."""
    store = KnowledgeStore(str(tmp_path / "acceptance.db"))
    imported: set[str] = set()
    for entry in _load_expectations():
        for fixture_name in entry["fixtures"]:
            fixture = LIFECYCLE_DIR / str(fixture_name)
            if str(fixture) not in imported:
                import_history(store, fixture, "membox-history-jsonl", project=_PROJECT)
                imported.add(str(fixture))
    return store


def _activation_expected(activation_status: str) -> bool:
    """Map gold activation_status to the bool expressed by activation_passes.

    ``active_unit`` → True (the unit passes activation).
    ``trace_only`` / ``unit_candidate`` → False (does not pass activation as an
    active unit; note that ``unit_candidate`` means the gate extracts it but
    activation_passes returns False due to confidence/score thresholds).
    """
    return activation_status == "active_unit"


def _compute_metrics(
    store: KnowledgeStore,
) -> tuple[float, float, float, dict[str, dict[str, object]]]:
    """Compute precision, recall, type_accuracy and per-entry results.

    Returns:
        Tuple of (precision, recall, type_accuracy, results).
        ``results`` maps entry id to a dict with gold/gate values.
    """
    expectations = _load_expectations()

    tp = 0
    fp = 0
    fn = 0
    type_correct = 0
    type_total = 0
    results: dict[str, dict[str, object]] = {}

    for entry in expectations:
        eid = str(entry["id"])
        expected = entry["expected"]
        gold_extract: bool = bool(expected["triage_should_extract"])
        gold_type: str = str(expected["unit_type"])
        gold_activation_status: str = str(expected["activation_status"])
        gold_activation_expected = _activation_expected(gold_activation_status)

        # Triage every referenced source ref; take the "best" decision
        # (first one that says should_extract=True, else the first one overall).
        best_decision = None
        best_activation = False
        gate_extract = False

        for ref in expected["source_refs"]:
            row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
            assert row is not None, f"trace {ref['trace_id']} not found in store for entry {eid}"
            decision = triage_trace(row["text"], role=row["role"])
            ap = activation_passes(decision, has_source=True)
            if best_decision is None:
                best_decision = decision
                best_activation = ap
            if decision.should_extract:
                gate_extract = True
                if not best_decision.should_extract:
                    best_decision = decision
                    best_activation = ap

        assert best_decision is not None

        results[eid] = {
            "gold_extract": gold_extract,
            "gate_extract": gate_extract,
            "gold_type": gold_type,
            "gate_type": best_decision.unit_type.value,
            "gold_activation": gold_activation_expected,
            "gate_activation": best_activation,
        }

        if gate_extract and gold_extract:
            tp += 1
        elif gate_extract and not gold_extract:
            fp += 1
        elif not gate_extract and gold_extract:
            fn += 1

        if gold_extract:
            type_total += 1
            if gate_extract and best_decision.unit_type.value == gold_type:
                type_correct += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    type_accuracy = type_correct / type_total if type_total > 0 else 1.0
    return precision, recall, type_accuracy, results


# ---------------------------------------------------------------------------
# Main acceptance test — metrics that currently pass
# ---------------------------------------------------------------------------


def test_c5_gate_acceptance_precision_and_dup_rate(tmp_path: Path) -> None:
    """Gate achieves perfect precision and zero duplicate rate today.

    Recall and type_accuracy have known gaps (c4, c1, c5, c7) that are
    asserted individually as xfail tests below.  The four metrics are
    always printed so CI output records the current state.
    """
    store = _build_store(tmp_path)
    precision, recall, type_accuracy, results = _compute_metrics(store)

    # ------------------------------------------------------------------
    # Duplicate rate: drive two full triage+extract passes via CLI runner
    # ------------------------------------------------------------------
    db = str(tmp_path / "dup_rate.db")
    dup_store = KnowledgeStore(db)
    imported_dup: set[str] = set()
    for entry in _load_expectations():
        for fixture_name in entry["fixtures"]:
            fixture = LIFECYCLE_DIR / str(fixture_name)
            if str(fixture) not in imported_dup:
                import_history(dup_store, fixture, "membox-history-jsonl", project=_PROJECT)
                imported_dup.add(str(fixture))

    cli = CliRunner()

    def _apply_triage_extract(db_path: str) -> None:
        r_triage = cli.invoke(
            app,
            ["memory", "triage", "--db", db_path, "--project", _PROJECT, "--apply"],
        )
        assert r_triage.exit_code == 0, f"triage failed: {r_triage.output}"
        r_extract = cli.invoke(
            app,
            ["memory", "extract", "--db", db_path, "--project", _PROJECT, "--apply"],
        )
        assert r_extract.exit_code == 0, f"extract failed: {r_extract.output}"

    _apply_triage_extract(db)
    units_after_first: int = (
        dup_store._conn().execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
    )

    _apply_triage_extract(db)
    units_after_second: int = (
        dup_store._conn().execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
    )

    dup_rate = (
        (units_after_second - units_after_first) / units_after_first
        if units_after_first > 0
        else 0.0
    )

    # ------------------------------------------------------------------
    # Print all four metrics unconditionally for CI visibility
    # ------------------------------------------------------------------
    print(f"\n=== C5 Gate Acceptance Metrics (gate={GATE_VERSION}) ===")
    gate_positives = len([r for r in results.values() if r["gate_extract"]])
    print(f"  triage precision  : {precision:.4f}  (gate positives={gate_positives})")
    print(f"  triage recall     : {recall:.4f}")
    print(f"  type accuracy     : {type_accuracy:.4f}")
    print(
        f"  duplicate rate    : {dup_rate:.4f}"
        f"  (after={units_after_second}, before={units_after_first})"
    )
    print("=== Per-entry detail ===")
    for eid, r in results.items():
        match = (
            "OK"
            if r["gate_extract"] == r["gold_extract"]
            and (not r["gold_extract"] or r["gate_type"] == r["gold_type"])
            else "MISMATCH"
        )
        print(
            f"  {eid}: extract={r['gate_extract']}(gold={r['gold_extract']}) "
            f"type={r['gate_type']}(gold={r['gold_type']}) [{match}]"
        )

    # ------------------------------------------------------------------
    # Assertions: precision and duplicate rate currently pass
    # ------------------------------------------------------------------
    assert precision == 1.0, (
        f"Precision {precision:.4f} < 1.0: gate produced FP on entries "
        f"{[eid for eid, r in results.items() if r['gate_extract'] and not r['gold_extract']]}"
    )
    assert dup_rate == 0.0, (
        f"Duplicate rate {dup_rate:.4f} != 0.0: "
        f"{units_after_second - units_after_first} extra units after second apply"
    )


# ---------------------------------------------------------------------------
# Per-entry xfail stubs for known gate disagreements
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "c4_superseded_fact: both source messages ('Fact: retrieval is graph-only' and "
        "'Fact: retrieval uses graph plus FTS fusion') score as weak_context_only because "
        "neither contains durable-change or explicit-memory signals.  Gold expects "
        "triage_should_extract=True.  Gate gap: the 'Fact:' prefix alone is not a "
        "heuristic trigger; requires either an explicit 'remember' or durable-change "
        "keyword to fire.  Fix: either add 'fact:' as a durable trigger or strengthen "
        "the fixture text.  Do NOT weaken the expectation."
    ),
)
def test_c4_superseded_fact_gate_gap(tmp_path: Path) -> None:
    """c4: gate misses both source refs (FN) — triage recall gap.

    Blocked by the weak_context_only reason on both c4 fixture messages.
    """
    store = _build_store(tmp_path)
    c4_entry = next(e for e in _load_expectations() if e["id"] == "c4_superseded_fact")
    expected = c4_entry["expected"]
    any_extract = False
    for ref in expected["source_refs"]:
        row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
        assert row is not None
        d = triage_trace(row["text"], role=row["role"])
        if d.should_extract:
            any_extract = True
    # This assertion is expected to FAIL (xfail): the gate currently returns
    # should_extract=False for both c4 refs.
    assert any_extract is True, "c4 gate gap: no source ref passes should_extract"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "c1_explicit_rules: gate returns unit_type=procedure, gold expects preference.  "
        "The fixture text uses 'always … before' which triggers procedure signals "
        "before preference signals in _infer_unit_type.  Fix: reorder inference or "
        "add a stronger preference keyword to the fixture.  Do NOT change expectations.yaml."
    ),
)
def test_c1_explicit_rules_type_mismatch(tmp_path: Path) -> None:
    """c1: gate infers unit_type=procedure; gold expects preference — type accuracy gap."""
    store = _build_store(tmp_path)
    c1_entry = next(e for e in _load_expectations() if e["id"] == "c1_explicit_rules")
    ref = c1_entry["expected"]["source_refs"][0]
    row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    # This assertion is expected to FAIL (xfail): gate returns procedure not preference.
    assert d.unit_type == MemoryUnitType.PREFERENCE, (
        f"c1 type gap: got {d.unit_type.value}, expected preference"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "c5_repeated_failure_learning: primary source ref (msg:m1) has type=preference "
        "because 'always' fires before procedure/fix signals.  Gold expects procedure.  "
        "The event ref (evt) scores weak_context_only.  Fix: reorder inference in "
        "_infer_unit_type so that fix/procedure signals beat 'always'-based preference "
        "when failure context is also present.  Do NOT change expectations.yaml."
    ),
)
def test_c5_type_mismatch(tmp_path: Path) -> None:
    """c5: gate infers preference for the message ref; gold expects procedure — type accuracy gap."""
    store = _build_store(tmp_path)
    c5_entry = next(e for e in _load_expectations() if e["id"] == "c5_repeated_failure_learning")
    # The message source ref is the one that passes; check its type.
    msg_ref = next(r for r in c5_entry["expected"]["source_refs"] if r["trace_kind"] == "message")
    row = store.get_trace_text(str(msg_ref["trace_kind"]), str(msg_ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    assert d.should_extract is True  # gate does say extract=True
    # This assertion is expected to FAIL (xfail): gate returns preference not procedure.
    assert d.unit_type == MemoryUnitType.PROCEDURE, (
        f"c5 type gap: got {d.unit_type.value}, expected procedure"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "c7_user_correction: correction-old message triggers should_extract=True with "
        "type=context (not decision), and correction-new message returns "
        "should_extract=False (weak_context_only).  Gold expects triage_should_extract=True "
        "and unit_type=decision.  Two gaps: (1) '更正:' (correction) keyword is not a "
        "durable-change signal so the new message does not pass; (2) old message is typed "
        "as context, not decision.  Fix: add '更正' to correction/durable signals and "
        "improve _infer_unit_type for correction context.  Do NOT change expectations.yaml."
    ),
)
def test_c7_user_correction_type_mismatch(tmp_path: Path) -> None:
    """c7: gate infers context (old) / rejects new — type and classification gap."""
    store = _build_store(tmp_path)
    c7_entry = next(e for e in _load_expectations() if e["id"] == "c7_user_correction")
    # Check the new correction source ref (should be should_extract=True, type=decision)
    new_ref = next(
        r for r in c7_entry["expected"]["source_refs"] if "correction-new" in str(r["trace_id"])
    )
    row = store.get_trace_text(str(new_ref["trace_kind"]), str(new_ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    # This assertion is expected to FAIL (xfail): gate returns should_extract=False.
    assert d.should_extract is True, (
        f"c7 new-correction gate gap: should_extract=False (reason={d.reason})"
    )
    assert d.unit_type == MemoryUnitType.DECISION, (
        f"c7 type gap: got {d.unit_type.value}, expected decision"
    )
