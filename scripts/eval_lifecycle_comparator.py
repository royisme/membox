"""Offline eval harness for the lifecycle LLM-comparator protocol.

The harness replays captured comparator scores from a committed synthetic corpus
and compares the v4 comparator-enabled consolidation path against human labels.
It deliberately avoids network calls: live LLM judging is outside CI, while this
script proves the injected comparator path, thresholding, and reporting stay
stable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Literal, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from membox.core.consolidate import build_consolidation_plan  # noqa: E402
from membox.core.triage import GATE_VERSION  # noqa: E402
from membox.model.schema import (  # noqa: E402
    MemorySourceKind,
    MemoryTemporalType,
    MemoryUnitRecord,
    MemoryUnitSource,
    MemoryUnitStatus,
    MemoryUnitType,
)
from membox.services.extraction import ComparatorScore  # noqa: E402

Label = Literal["keep", "drop"]


class ReplayComparator:
    """Comparator implementation that replays captured per-unit scores."""

    def __init__(self, scores: dict[int, float]) -> None:
        self._scores = scores

    def rescore_candidates(
        self,
        candidates: list[MemoryUnitRecord],
        surrounding_units: list[MemoryUnitRecord],
    ) -> list[ComparatorScore]:
        """Return captured scores for candidate units."""
        _ = surrounding_units
        return [
            ComparatorScore(unit_id=unit.id or 0, score=self._scores[unit.id or 0])
            for unit in candidates
            if (unit.id or 0) in self._scores
        ]


def _source(unit_id: int) -> MemoryUnitSource:
    """Return deterministic manual source metadata for a synthetic unit."""
    ref = f"comparator-fixture:{unit_id}"
    return MemoryUnitSource(
        source_kind=MemorySourceKind.MANUAL,
        source_ref=ref,
        source_message_id=ref,
        quote=ref,
    )


def _unit(raw: dict[str, Any]) -> MemoryUnitRecord:
    """Build one memory unit from a YAML case record."""
    unit_id = int(raw["id"])
    unit_type = MemoryUnitType(str(raw["unit_type"]))
    # M4 Part A2 added the rationale gate. Synthetic comparator cases use
    # MANUAL sources (agent-extracted by definition), so they would be
    # pre-rejected without a why/how/next. Provide stable defaults so the
    # comparator itself is what the eval exercises.
    why = "comparator eval fixture — rationale not under test"
    how_to_apply = "comparator eval recipe" if unit_type == MemoryUnitType.PROCEDURE else None
    next_step = (
        "comparator eval next step"
        if unit_type in (MemoryUnitType.PROCEDURE, MemoryUnitType.PLAN)
        else None
    )
    return MemoryUnitRecord(
        id=unit_id,
        project="membox-lifecycle",
        unit_type=unit_type,
        status=MemoryUnitStatus(str(raw["status"])),
        title=str(raw["title"]),
        content=str(raw["content"]),
        context=f"offline comparator eval case {unit_id}",
        importance_score=float(raw.get("importance_score", 0.80)),
        confidence_score=float(raw.get("confidence_score", 0.75)),
        temporal_type=MemoryTemporalType.UNKNOWN,
        valid_to=None if raw.get("valid_to") is None else str(raw["valid_to"]),
        labels=[str(label) for label in raw.get("labels", [])],
        sources=[_source(unit_id)],
        why=why,
        how_to_apply=how_to_apply,
        next_step=next_step,
    )


def _load_cases(path: Path) -> tuple[float, float, list[dict[str, Any]]]:
    """Load comparator cases and thresholds from YAML."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict)
    score_threshold = float(data.get("score_threshold", 0.50))
    min_agreement = float(data.get("min_agreement", 0.80))
    cases = data.get("cases", [])
    assert isinstance(cases, list)
    return score_threshold, min_agreement, cast("list[dict[str, Any]]", cases)


def _run_case(raw: dict[str, Any], threshold: float) -> tuple[str, Label, Label, str]:
    """Run one comparator eval case and return `(id, expected, predicted, reason)`."""
    case_id = str(raw["id"])
    expected = cast("Label", str(raw["human_label"]))
    subject = _unit(cast("dict[str, Any]", raw["unit"]))
    surrounding_raw = cast("list[dict[str, Any]]", raw.get("surrounding_units", []))
    units = [subject, *[_unit(item) for item in surrounding_raw]]
    subject_id = subject.id
    assert subject_id is not None

    counts = {unit.id or 0: int(raw.get("independent_sources", 1)) for unit in units}
    default_plan = build_consolidation_plan(units, counts)
    default_transition_ids = {
        transition.unit_id for transition in default_plan.ordered_transitions()
    }
    if subject_id not in default_transition_ids:
        return case_id, expected, "drop", "no default transition before comparator"

    plan = build_consolidation_plan(
        units,
        counts,
        comparator=ReplayComparator({subject_id: float(raw["llm_score"])}),
        comparator_threshold=threshold,
    )
    transition_ids = {transition.unit_id for transition in plan.ordered_transitions()}
    predicted: Label = "keep" if subject_id in transition_ids else "drop"
    return case_id, expected, predicted, f"score={float(raw['llm_score']):.2f}"


def main(argv: list[str] | None = None) -> int:
    """Run the offline comparator agreement eval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "eval" / "lifecycle" / "comparator_cases.yaml",
        help="Path to comparator case YAML.",
    )
    parser.add_argument(
        "--min-agreement",
        type=float,
        default=None,
        help="Override the YAML threshold for required agreement.",
    )
    args = parser.parse_args(argv)

    score_threshold, min_agreement, cases = _load_cases(args.cases)
    required = min_agreement if args.min_agreement is None else args.min_agreement
    if not cases:
        _err("ERROR: no comparator cases found")
        return 1

    rows = [_run_case(case, score_threshold) for case in cases]
    correct = sum(1 for _case_id, expected, predicted, _reason in rows if expected == predicted)
    agreement = correct / len(rows)

    _out(f"Lifecycle comparator eval (gate={GATE_VERSION}, score_threshold={score_threshold:.2f})")
    for case_id, expected, predicted, reason in rows:
        mark = "OK" if expected == predicted else "MISS"
        _out(f"{mark} {case_id}: expected={expected} predicted={predicted} {reason}")
    _out(f"agreement={agreement:.3f} ({correct}/{len(rows)}) required>={required:.3f}")

    if agreement < required:
        _err("GATE FAILED: comparator agreement below threshold")
        return 1
    return 0


def _out(message: str) -> None:
    """Write one line to stdout."""
    sys.stdout.write(f"{message}\n")


def _err(message: str) -> None:
    """Write one line to stderr."""
    sys.stderr.write(f"{message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
