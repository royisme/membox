"""C5 acceptance harness for the Phase C heuristic triage gate.

Runs the C1 fixture corpus end-to-end and records four metrics:

- **triage precision** - rejected chatter must not become units (no false positives)
- **triage recall**   - explicit rules must be selected (no false negatives)
- **type accuracy**   - predicted unit_type matches gold expectation
- **duplicate rate**  - units after second apply equals units after first apply

The harness asserts exact agreement (all metrics == 1.0 / 0.0).  The four
heuristic-v1 disagreements (c4, c1, c5, c7) were resolved by the heuristic-v2
keyword tuning and are kept below as plain regression tests; if the gate ever
regresses, fix the gate — do not edit ``expectations.yaml`` to make it pass.

Multi-source entries (c3, c4, c5, c6, c7): the harness treats each source ref
independently.  For *precision/recall*, an entry is considered ``should_extract``
if **any** of its source refs passes the gate (consistent with how the real CLI
pipeline works — any matching trace produces a unit candidate).  For *type
accuracy*, an entry counts as correct if **any** passing source ref predicts the
gold type — multi-ref lifecycle entries can legitimately extract refs of
different types (c3 extracts a plan ref and a decision ref; gold names the
surviving unit's type).  If no ref passes, the entry is excluded from type
accuracy (already counted as a recall miss).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

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
        extracted_types: set[str] = set()

        for ref in expected["source_refs"]:
            row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
            assert row is not None, f"trace {ref['trace_id']} not found in store for entry {eid}"
            decision = triage_trace(
                row["text"], role=row["role"], trace_kind=str(ref["trace_kind"])
            )
            ap = activation_passes(decision, has_source=True)
            if best_decision is None:
                best_decision = decision
                best_activation = ap
            if decision.should_extract:
                gate_extract = True
                extracted_types.add(decision.unit_type.value)
                if not best_decision.should_extract:
                    best_decision = decision
                    best_activation = ap

        assert best_decision is not None

        results[eid] = {
            "gold_extract": gold_extract,
            "gate_extract": gate_extract,
            "gold_type": gold_type,
            "gate_type": best_decision.unit_type.value,
            "gate_types": sorted(extracted_types) or [best_decision.unit_type.value],
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
            if gate_extract and gold_type in extracted_types:
                type_correct += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    type_accuracy = type_correct / type_total if type_total > 0 else 1.0
    return precision, recall, type_accuracy, results


# ---------------------------------------------------------------------------
# Main acceptance test — metrics that currently pass
# ---------------------------------------------------------------------------


def test_c5_gate_acceptance_precision_and_dup_rate(tmp_path: Path) -> None:
    """Gate achieves exact agreement with the C1 gold expectations.

    heuristic-v1 had four documented disagreements (c4, c1, c5, c7);
    heuristic-v2's keyword tuning resolved them, so all four metrics are
    asserted at their targets.  The metrics are always printed so CI
    output records the current state.
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
        gate_types = cast("list[str]", r["gate_types"])
        match = (
            "OK"
            if r["gate_extract"] == r["gold_extract"]
            and (not r["gold_extract"] or r["gold_type"] in gate_types)
            else "MISMATCH"
        )
        print(
            f"  {eid}: extract={r['gate_extract']}(gold={r['gold_extract']}) "
            f"types={gate_types}(gold={r['gold_type']}) [{match}]"
        )

    # ------------------------------------------------------------------
    # Assertions: precision and duplicate rate currently pass
    # ------------------------------------------------------------------
    assert precision == 1.0, (
        f"Precision {precision:.4f} < 1.0: gate produced FP on entries "
        f"{[eid for eid, r in results.items() if r['gate_extract'] and not r['gold_extract']]}"
    )
    assert recall == 1.0, (
        f"Recall {recall:.4f} < 1.0: gate missed entries "
        f"{[eid for eid, r in results.items() if r['gold_extract'] and not r['gate_extract']]}"
    )
    type_mismatches = [
        eid
        for eid, r in results.items()
        if r["gold_extract"] and r["gold_type"] not in cast("list[str]", r["gate_types"])
    ]
    assert type_accuracy == 1.0, (
        f"Type accuracy {type_accuracy:.4f} < 1.0: mismatched entries {type_mismatches}"
    )
    assert dup_rate == 0.0, (
        f"Duplicate rate {dup_rate:.4f} != 0.0: "
        f"{units_after_second - units_after_first} extra units after second apply"
    )


def test_phase_d_consolidation_acceptance_statuses(tmp_path: Path) -> None:
    """Consolidation matches Phase D lifecycle fixture expectations."""
    db = str(tmp_path / "phase_d.db")
    store = KnowledgeStore(db)
    imported: set[str] = set()
    for entry in _load_expectations():
        for fixture_name in entry["fixtures"]:
            fixture = LIFECYCLE_DIR / str(fixture_name)
            if str(fixture) not in imported:
                import_history(store, fixture, "membox-history-jsonl", project=_PROJECT)
                imported.add(str(fixture))

    cli = CliRunner()
    for command in (
        ["memory", "triage", "--db", db, "--project", _PROJECT, "--apply"],
        ["memory", "extract", "--db", db, "--project", _PROJECT, "--apply"],
    ):
        result = cli.invoke(app, command)
        assert result.exit_code == 0, result.output
    dry_run = cli.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", _PROJECT, "--dry-run"],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "conflict review" in dry_run.output
    assert "life-c6-conflict-a" in dry_run.output
    assert "life-c6-conflict-b" in dry_run.output
    assert (
        store._conn()
        .execute("SELECT COUNT(*) FROM meta WHERE key=?;", (f"lifecycle_lease:{_PROJECT}",))
        .fetchone()[0]
        == 0
    )

    apply = cli.invoke(
        app,
        ["memory", "consolidate", "--db", db, "--project", _PROJECT, "--apply"],
    )
    assert apply.exit_code == 0, apply.output

    by_source = _memory_status_by_source(store)
    assert by_source["membox-capture:life-c4-old:msg:m1"][0] == "superseded"
    assert by_source["membox-capture:life-c4-new:msg:m1"][0] == "active_unit"
    assert by_source["membox-capture:life-c5-failure:msg:m1"][0] == "crystal_candidate"
    assert by_source["membox-capture:life-c6-conflict-a:msg:m1"][0] == "active_unit"
    assert by_source["membox-capture:life-c6-conflict-b:msg:m1"][0] == "active_unit"
    assert by_source["membox-capture:life-c7-correction-old:msg:m1"][0] == "superseded"
    assert by_source["membox-capture:life-c7-correction-new:msg:m1"][0] == "active_unit"

    old_fact = by_source["membox-capture:life-c4-old:msg:m1"]
    new_fact = by_source["membox-capture:life-c4-new:msg:m1"]
    old_correction = by_source["membox-capture:life-c7-correction-old:msg:m1"]
    new_correction = by_source["membox-capture:life-c7-correction-new:msg:m1"]
    assert old_fact[1] == new_fact[2]
    assert old_correction[1] == new_correction[2]

    # ------------------------------------------------------------------
    # D5 scope: c3 plan→decision supersession — the v3 declared-plan band
    # extracts the tentative plan, and consolidation supersedes it with
    # the later decision unit.
    assert by_source["membox-capture:life-c3-plan:msg:m1"][0] == "superseded"
    assert by_source["membox-capture:life-c3-decision:msg:m1"][0] == "active_unit", (
        "c3 decision unit must remain active_unit after consolidation"
    )
    old_plan = by_source["membox-capture:life-c3-plan:msg:m1"]
    new_decision = by_source["membox-capture:life-c3-decision:msg:m1"]
    assert old_plan[1] == new_decision[2]

    # ------------------------------------------------------------------
    # D5 scope: c1 not_applicable — explicit rule stays active, not superseded
    assert by_source["membox-capture:life-c1-rules:msg:m1"][0] == "active_unit", (
        "c1 explicit rule must stay active_unit (phase_d_status: not_applicable)"
    )
    assert by_source["membox-capture:life-c1-rules:msg:m1"][1] is None, (
        "c1 explicit rule must not be superseded"
    )

    # ------------------------------------------------------------------
    # D5 scope: c2 not_applicable — ephemeral chatter never extracted
    # (triage_should_extract: false means no memory_units row at all)
    assert not any(src.startswith("membox-capture:life-c2-chatter") for src in by_source), (
        "c2 ephemeral chatter must not produce any memory_unit (rejected at triage)"
    )

    # ------------------------------------------------------------------
    # D5 scope: c8/c9 v3 gate rejects — no units must exist
    assert not any(src.startswith("membox-capture:life-c8-toolnoise") for src in by_source), (
        "c8 tool output noise must not produce any memory_unit (rejected by v3 gate)"
    )
    assert not any(src.startswith("membox-capture:life-c9-template") for src in by_source), (
        "c9 harness template noise must not produce any memory_unit (rejected by v3 gate)"
    )

    # ------------------------------------------------------------------
    # D5 scope: crystal precision — every crystal/crystal_candidate unit
    # must belong to a session whose expectations entry has
    # phase_d_status crystal_candidate.
    #
    # We match at the session level (prefix before the first ":msg:" or
    # ":evt:") rather than exact trace_id to account for multi-message
    # sessions where the extractor may create units from messages not
    # enumerated in source_refs (e.g. c5:msg:m2 alongside c5:msg:m1).
    expectations = _load_expectations()
    crystal_statuses = {"crystal", "crystal_candidate"}

    def _session_prefix(source_ref: str) -> str:
        """Return the session portion of a source_ref (up to :msg: or :evt:)."""
        for sep in (":msg:", ":evt:"):
            idx = source_ref.find(sep)
            if idx >= 0:
                return source_ref[:idx]
        return source_ref

    # Build the set of session prefixes that are allowed to crystallize
    candidate_session_prefixes: set[str] = set()
    for entry in expectations:
        if entry["expected"].get("phase_d_status") == "crystal_candidate":
            for ref in entry["expected"]["source_refs"]:
                candidate_session_prefixes.add(_session_prefix(str(ref["trace_id"])))

    # Count how many crystal/crystal_candidate units map to candidate sessions
    crystal_units = [
        src
        for src, (status, _superseded_by, _uid) in by_source.items()
        if status in crystal_statuses
    ]
    hits = sum(1 for src in crystal_units if _session_prefix(src) in candidate_session_prefixes)
    total = len(crystal_units)
    ratio = hits / total if total > 0 else 1.0
    print(f"phase-D crystal precision: {hits}/{total} = {ratio:.2f}")
    assert ratio == 1.0, (
        f"crystal precision {ratio:.2f} < 1.0: non-candidate sessions promoted — "
        f"unexpected crystals from sources "
        f"{[s for s in crystal_units if _session_prefix(s) not in candidate_session_prefixes]}"
    )


def _memory_status_by_source(store: KnowledgeStore) -> dict[str, tuple[str, int | None, int]]:
    """Return memory unit status metadata keyed by source_ref."""
    rows = (
        store._conn()
        .execute(
            """
            SELECT mus.source_ref, mu.status, mu.superseded_by, mu.id
            FROM memory_unit_sources mus
            JOIN memory_units mu ON mu.id=mus.unit_id
            """
        )
        .fetchall()
    )
    return {
        str(row[0]): (str(row[1]), None if row[2] is None else int(row[2]), int(row[3]))
        for row in rows
    }


# ---------------------------------------------------------------------------
# Per-entry regression tests for the heuristic-v1 disagreements fixed in v2
# ---------------------------------------------------------------------------


def test_c4_declared_fact_extracts(tmp_path: Path) -> None:
    """c4: declared facts about durable topics must extract (v1 missed both refs)."""
    store = _build_store(tmp_path)
    c4_entry = next(e for e in _load_expectations() if e["id"] == "c4_superseded_fact")
    for ref in c4_entry["expected"]["source_refs"]:
        row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
        assert row is not None
        d = triage_trace(row["text"], role=row["role"], trace_kind=str(ref["trace_kind"]))
        assert d.should_extract is True, f"c4 {ref['trace_id']}: rejected (reason={d.reason})"
        assert d.unit_type == MemoryUnitType.FACT, (
            f"c4 {ref['trace_id']}: got {d.unit_type.value}, expected fact"
        )


def test_c1_explicit_rule_types_as_preference(tmp_path: Path) -> None:
    """c1: an explicit user rule types as preference (v1 returned procedure)."""
    store = _build_store(tmp_path)
    c1_entry = next(e for e in _load_expectations() if e["id"] == "c1_explicit_rules")
    ref = c1_entry["expected"]["source_refs"][0]
    row = store.get_trace_text(str(ref["trace_kind"]), str(ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    assert d.should_extract is True
    assert d.unit_type == MemoryUnitType.PREFERENCE, (
        f"c1: got {d.unit_type.value}, expected preference"
    )


def test_c5_failure_remedy_types_as_procedure(tmp_path: Path) -> None:
    """c5: 'always verify X' beside a failure types as procedure, not preference."""
    store = _build_store(tmp_path)
    c5_entry = next(e for e in _load_expectations() if e["id"] == "c5_repeated_failure_learning")
    msg_ref = next(r for r in c5_entry["expected"]["source_refs"] if r["trace_kind"] == "message")
    row = store.get_trace_text(str(msg_ref["trace_kind"]), str(msg_ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    assert d.should_extract is True
    assert d.unit_type == MemoryUnitType.PROCEDURE, (
        f"c5: got {d.unit_type.value}, expected procedure"
    )


def test_c7_correction_extracts_as_decision(tmp_path: Path) -> None:
    """c7: a 更正/correction message extracts and types as decision (v1 rejected it)."""
    store = _build_store(tmp_path)
    c7_entry = next(e for e in _load_expectations() if e["id"] == "c7_user_correction")
    new_ref = next(
        r for r in c7_entry["expected"]["source_refs"] if "correction-new" in str(r["trace_id"])
    )
    row = store.get_trace_text(str(new_ref["trace_kind"]), str(new_ref["trace_id"]))
    assert row is not None
    d = triage_trace(row["text"], role=row["role"])
    assert d.should_extract is True, f"c7: should_extract=False (reason={d.reason})"
    assert d.unit_type == MemoryUnitType.DECISION, f"c7: got {d.unit_type.value}, expected decision"


# ---------------------------------------------------------------------------
# Per-case regression tests for the D0 real-trace false positives fixed in v3
# ---------------------------------------------------------------------------

_RAW_TOOL_BODY = (
    "Chunk ID: ab12cd\n"
    "Wall time: 0.18 seconds\n"
    "Process exited with code 2\n"
    "Original token count: 120\n"
    "Output: HELD — reply NOT sent. First check the queue, "
    "then run `status --all` again."
)

_JSON_CMD_BODY = (
    '{"cmd":"reply thread-123 \'I will take the first task, then wait for the '
    'next step\'","workdir":"/home/dev/project"}'
)

_WAKEUP_TEMPLATE = (
    "You've been woken because there is new activity in your group threads, "
    "and the upstream triage already decided you should respond — your job is "
    "to DO it, not to re-judge whether to. First read the unread items "
    "(ALREADY FETCHED — no need to re-run `inbox`), then post your reply."
)


def test_v3_raw_tool_event_with_procedure_words_rejected() -> None:
    """Family A: raw tool output bodies must never extract as events."""
    d = triage_trace(_RAW_TOOL_BODY, role="tool_result", trace_kind="event")
    assert d.should_extract is False, f"raw tool body extracted (reason={d.reason})"


def test_v3_json_cmd_payload_event_rejected() -> None:
    """Family A: JSON command payloads must never extract as events."""
    d = triage_trace(_JSON_CMD_BODY, role="tool_result", trace_kind="event")
    assert d.should_extract is False, f"json cmd payload extracted (reason={d.reason})"


def test_v3_wakeup_template_message_rejected() -> None:
    """Family B: the harness wake-up template message must not extract."""
    d = triage_trace(_WAKEUP_TEMPLATE, role="user", trace_kind="message")
    assert d.should_extract is False, f"wake-up template extracted (reason={d.reason})"


def test_v3_failure_procedure_message_still_extracts() -> None:
    """Guard against over-tightening: c5-style failure remedies still extract."""
    text = (
        "We hit the stale worktree migration-numbering failure again. "
        "Always verify latest_version() before adding a migration."
    )
    d = triage_trace(text, role="user", trace_kind="message")
    assert d.should_extract is True, f"c5-style remedy rejected (reason={d.reason})"
    assert d.unit_type == MemoryUnitType.PROCEDURE


def test_v3_explicit_rule_message_still_extracts() -> None:
    """Guard against over-tightening: c1-style explicit rules still extract."""
    text = "Remember this rule: always run the lifecycle fixtures before tuning the gate."
    d = triage_trace(text, role="user", trace_kind="message")
    assert d.should_extract is True, f"explicit rule rejected (reason={d.reason})"


def test_v3_declared_durable_plan_extracts() -> None:
    """c3: a declared plan about a durable topic extracts as a plan unit."""
    text = (
        "Plan: maybe put memory unit tables in migration 8 after lifecycle "
        "fixtures are merged. Do not implement this until we confirm the schema."
    )
    d = triage_trace(text, role="user", trace_kind="message")
    assert d.should_extract is True, f"declared plan rejected (reason={d.reason})"
    assert d.unit_type == MemoryUnitType.PLAN, f"c3: got {d.unit_type.value}, expected plan"
    assert d.reason == "declared_durable_plan"


def test_v3_correction_event_still_extracts() -> None:
    """Corrections survive the v3 event guard."""
    d = triage_trace(
        "correction: the default branch is main, not master",
        role="tool_result",
        trace_kind="event",
    )
    assert d.should_extract is True, f"correction event rejected (reason={d.reason})"
