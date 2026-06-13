"""Pure consolidation policy for lifecycle Phase D memory units."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from membox.model.schema import MemorySourceKind, MemoryUnitRecord, MemoryUnitStatus, MemoryUnitType

if TYPE_CHECKING:
    from membox.services.extraction import LLMComparator

CRYSTAL_SOURCE_THRESHOLD = 3
"""Independent-source count required for automatic crystal promotion."""

CRYSTAL_DECISION_CONFIDENCE_THRESHOLD = 0.90
"""Decision confidence required for automatic crystal promotion."""

CRYSTAL_DECISION_IMPORTANCE_THRESHOLD = 0.80
"""Decision importance required for automatic crystal promotion."""

CRYSTAL_CONFIDENCE_INCREMENT = 0.05
"""Confidence increase per newly attached independent source."""

CRYSTAL_CONFIDENCE_CAP = 0.95
"""Maximum confidence produced by score evolution."""

MIN_CONTENT_LENGTH = 3
"""Small lower bound used by the validator to reject empty-looking claims."""

MAX_CONTENT_LENGTH = 4000
"""Conservative upper bound for compact unit/crystal content."""

_CORRECTION_TERMS = ("correction", "corrected", "updated", "stale", "instead", "更正")
_CONTRAST_TERMS = ("instead of", "rather than", "conflict", "conflicting", "而不是", "冲突")
_STRONG_CORRECTION_TERMS = ("correction", "corrected", "stale", "supersede", "更正", "作废")
_REVIEW_HORIZON_TYPES = {MemoryUnitType.PLAN, MemoryUnitType.CONTEXT}


@dataclass(frozen=True)
class CrystalPolicyResult:
    """Decision made by the Phase D crystal-promotion policy."""

    eligible: bool
    reason: str
    independent_source_count: int


@dataclass(frozen=True)
class ConsolidationIssue:
    """Validator or decay issue surfaced during consolidation."""

    unit_id: int
    title: str
    reason: str


@dataclass(frozen=True)
class ConsolidationConflict:
    """Pair of units that should be reviewed by a human before merging."""

    left_id: int
    right_id: int
    left_title: str
    right_title: str
    reason: str
    source_refs: list[str]


@dataclass(frozen=True)
class ConsolidationTransition:
    """One audited status transition proposed by consolidation."""

    unit_id: int
    title: str
    to_status: MemoryUnitStatus
    reason: str
    superseded_by: int | None = None


@dataclass(frozen=True)
class ConsolidationPlan:
    """All actions and surfaced issues for one consolidation run."""

    promotions: list[ConsolidationTransition] = field(default_factory=list)
    candidates: list[ConsolidationTransition] = field(default_factory=list)
    demotions: list[ConsolidationTransition] = field(default_factory=list)
    conflicts: list[ConsolidationConflict] = field(default_factory=list)
    fts_pairs: list[ConsolidationConflict] = field(default_factory=list)
    supersessions: list[ConsolidationTransition] = field(default_factory=list)
    decay_archives: list[ConsolidationTransition] = field(default_factory=list)
    decay_reviews: list[ConsolidationIssue] = field(default_factory=list)
    validator_rejections: list[ConsolidationIssue] = field(default_factory=list)


def crystal_policy(unit: MemoryUnitRecord, independent_source_count: int) -> CrystalPolicyResult:
    """Return whether a unit satisfies the accepted Phase D crystal policy."""
    if not unit.sources:
        return CrystalPolicyResult(False, "no_source", independent_source_count)
    if has_explicit_user_confirmation(unit):
        return CrystalPolicyResult(True, "explicit_user_confirmation", independent_source_count)
    if independent_source_count >= CRYSTAL_SOURCE_THRESHOLD:
        return CrystalPolicyResult(True, "independent_source_count>=3", independent_source_count)
    if (
        unit.unit_type == MemoryUnitType.DECISION
        and unit.confidence_score >= CRYSTAL_DECISION_CONFIDENCE_THRESHOLD
        and unit.importance_score >= CRYSTAL_DECISION_IMPORTANCE_THRESHOLD
    ):
        return CrystalPolicyResult(True, "high_confidence_decision", independent_source_count)
    return CrystalPolicyResult(False, "below_crystal_threshold", independent_source_count)


def evolved_confidence(current: float, new_independent_sources: int) -> float:
    """Return confidence after score evolution for newly attached sources.

    Never decreases the score — if current already exceeds the cap, it is
    preserved as-is.
    """
    if new_independent_sources <= 0:
        return current
    return max(
        current,
        min(
            CRYSTAL_CONFIDENCE_CAP, current + CRYSTAL_CONFIDENCE_INCREMENT * new_independent_sources
        ),
    )


def build_consolidation_plan(
    units: list[MemoryUnitRecord],
    independent_counts: dict[int, int],
    *,
    fts_pair_ids: set[tuple[int, int]] | None = None,
    comparator: LLMComparator | None = None,
    comparator_threshold: float = 0.50,
) -> ConsolidationPlan:
    """Build a deterministic dry-run/apply plan for Phase D consolidation."""
    validator_rejections = validate_units(units)
    rejected_ids = {issue.unit_id for issue in validator_rejections}
    eligible_units = [unit for unit in units if unit.id is not None and unit.id not in rejected_ids]
    conflicts = detect_conflicts(eligible_units)
    fts_pairs = detect_fts_pairs(eligible_units, fts_pair_ids=fts_pair_ids)
    conflict_ids = {conflict.left_id for conflict in conflicts} | {
        conflict.right_id for conflict in conflicts
    }
    supersessions = detect_supersessions(eligible_units, conflict_ids=conflict_ids)
    superseded_ids = {transition.unit_id for transition in supersessions}

    promotions: list[ConsolidationTransition] = []
    candidates: list[ConsolidationTransition] = []
    demotions: list[ConsolidationTransition] = []
    decay_archives: list[ConsolidationTransition] = []
    decay_reviews: list[ConsolidationIssue] = []

    for unit in eligible_units:
        assert unit.id is not None
        if unit.id in superseded_ids or unit.id in conflict_ids:
            continue
        decay = decay_action(unit)
        if isinstance(decay, ConsolidationTransition):
            decay_archives.append(decay)
            continue
        if isinstance(decay, ConsolidationIssue):
            decay_reviews.append(decay)
            continue

        policy = crystal_policy(unit, independent_counts.get(unit.id, 0))
        if unit.status in {MemoryUnitStatus.ACTIVE_UNIT, MemoryUnitStatus.CRYSTAL_CANDIDATE}:
            if policy.eligible:
                promotions.append(
                    ConsolidationTransition(
                        unit.id,
                        unit.title,
                        MemoryUnitStatus.CRYSTAL,
                        f"crystal policy: {policy.reason}",
                    )
                )
                continue
            if unit.status == MemoryUnitStatus.ACTIVE_UNIT and should_be_crystal_candidate(unit):
                candidates.append(
                    ConsolidationTransition(
                        unit.id,
                        unit.title,
                        MemoryUnitStatus.CRYSTAL_CANDIDATE,
                        "recurring or failure-backed unit needs crystal review",
                    )
                )
                continue
            if (
                unit.status == MemoryUnitStatus.CRYSTAL_CANDIDATE
                and not should_be_crystal_candidate(unit)
            ):
                demotions.append(
                    ConsolidationTransition(
                        unit.id,
                        unit.title,
                        MemoryUnitStatus.ACTIVE_UNIT,
                        "crystal policy rejected candidate",
                    )
                )

    plan = ConsolidationPlan(
        promotions=promotions,
        candidates=candidates,
        demotions=demotions,
        conflicts=conflicts,
        fts_pairs=fts_pairs,
        supersessions=supersessions,
        decay_archives=decay_archives,
        decay_reviews=decay_reviews,
        validator_rejections=validator_rejections,
    )
    if comparator is None:
        return plan
    return _apply_llm_comparator(plan, eligible_units, comparator, comparator_threshold)


def has_explicit_user_confirmation(unit: MemoryUnitRecord) -> bool:
    """Return whether provenance shows explicit manual/user confirmation."""
    if any(source.source_kind == MemorySourceKind.MANUAL for source in unit.sources):
        return True
    text = f"{unit.title}\n{unit.content}\n{unit.context}".casefold()
    return "user-confirmed" in text or "owner confirmed" in text or "explicitly confirmed" in text


def should_be_crystal_candidate(unit: MemoryUnitRecord) -> bool:
    """Return whether an active unit should enter crystal review."""
    text = f"{unit.title}\n{unit.content}\n{unit.context}".casefold()
    return unit.unit_type in {MemoryUnitType.PROCEDURE, MemoryUnitType.LEARNING} and (
        "failure" in text
        or "failed" in text
        or "error" in text
        or "verify" in text
        or len(unit.sources) > 1
    )


def validate_units(units: list[MemoryUnitRecord]) -> list[ConsolidationIssue]:
    """Run the Phase D validator over units without mutating them."""
    issues: list[ConsolidationIssue] = []
    seen_titles: dict[tuple[str, str], int] = {}
    for unit in units:
        if unit.id is None:
            continue
        if not unit.sources:
            issues.append(ConsolidationIssue(unit.id, unit.title, "no source"))
        content_len = len(unit.content.strip())
        if content_len < MIN_CONTENT_LENGTH or content_len > MAX_CONTENT_LENGTH:
            issues.append(ConsolidationIssue(unit.id, unit.title, "content length out of bounds"))
        title_key = (unit.project, unit.title.strip().casefold())
        previous = seen_titles.get(title_key)
        if previous is not None:
            issues.append(
                ConsolidationIssue(unit.id, unit.title, f"duplicate title also used by {previous}")
            )
        else:
            seen_titles[title_key] = unit.id
        for source in unit.sources:
            if source.source_kind in {
                MemorySourceKind.MANUAL,
                MemorySourceKind.HISTORY_MESSAGE,
                MemorySourceKind.HISTORY_EVENT,
                MemorySourceKind.RELATION,
                MemorySourceKind.UNIT,
            }:
                continue
            if source.source_kind == MemorySourceKind.DOCUMENT:
                ref = source.source_ref
                if ref and Path(ref).is_absolute() and not Path(ref).exists():
                    issues.append(
                        ConsolidationIssue(
                            unit.id,
                            unit.title,
                            f"stale source path: {ref}",
                        )
                    )
        if unit.sources and _unsupported_claim(unit):
            issues.append(ConsolidationIssue(unit.id, unit.title, "unsupported claim heuristic"))
    return issues


def detect_conflicts(units: list[MemoryUnitRecord]) -> list[ConsolidationConflict]:
    """Surface deterministic conflict pairs; never merge or overwrite them."""
    active = [
        unit
        for unit in units
        if unit.id is not None
        and unit.status
        in {
            MemoryUnitStatus.ACTIVE_UNIT,
            MemoryUnitStatus.CRYSTAL_CANDIDATE,
            MemoryUnitStatus.CRYSTAL,
        }
    ]
    conflicts: list[ConsolidationConflict] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            assert left.id is not None and right.id is not None
            if left.project != right.project or not set(left.labels).intersection(right.labels):
                continue
            if _looks_conflicting(left, right):
                conflicts.append(
                    ConsolidationConflict(
                        left.id,
                        right.id,
                        left.title,
                        right.title,
                        "overlapping labels and topic with contrast signals",
                        sorted(_source_refs(left) | _source_refs(right)),
                    )
                )
                continue
    return conflicts


def detect_fts_pairs(
    units: list[MemoryUnitRecord],
    *,
    fts_pair_ids: set[tuple[int, int]] | None = None,
) -> list[ConsolidationConflict]:
    """Surface FTS review pairs without treating them as hard conflicts."""
    if not fts_pair_ids:
        return []
    active = [
        unit
        for unit in units
        if unit.id is not None
        and unit.status
        in {
            MemoryUnitStatus.ACTIVE_UNIT,
            MemoryUnitStatus.CRYSTAL_CANDIDATE,
            MemoryUnitStatus.CRYSTAL,
        }
    ]
    pairs: list[ConsolidationConflict] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            assert left.id is not None and right.id is not None
            if left.project != right.project or not set(left.labels).intersection(right.labels):
                continue
            if (left.id, right.id) in fts_pair_ids and _looks_fts_pair(left, right):
                pairs.append(
                    ConsolidationConflict(
                        left.id,
                        right.id,
                        left.title,
                        right.title,
                        "fts_pair token-overlap candidate",
                        sorted(_source_refs(left) | _source_refs(right)),
                    )
                )
    return pairs


def detect_supersessions(
    units: list[MemoryUnitRecord],
    *,
    conflict_ids: set[int],
) -> list[ConsolidationTransition]:
    """Find older units superseded by newer corrective units."""
    active = [
        unit
        for unit in units
        if unit.id is not None
        and unit.id not in conflict_ids
        and unit.status in {MemoryUnitStatus.ACTIVE_UNIT, MemoryUnitStatus.CRYSTAL}
    ]
    transitions: list[ConsolidationTransition] = []
    for older in active:
        assert older.id is not None
        newer_candidates = [
            unit
            for unit in active
            if unit.id is not None
            and unit.id > older.id
            and unit.project == older.project
            and set(unit.labels).intersection(older.labels)
            and _looks_like_replacement(older, unit)
        ]
        if not newer_candidates:
            continue
        newest = max(newer_candidates, key=lambda unit: unit.id or 0)
        assert newest.id is not None
        transitions.append(
            ConsolidationTransition(
                older.id,
                older.title,
                MemoryUnitStatus.SUPERSEDED,
                f"newer unit {newest.id} supersedes this memory",
                superseded_by=newest.id,
            )
        )
    return transitions


def decay_action(unit: MemoryUnitRecord) -> ConsolidationTransition | ConsolidationIssue | None:
    """Return the decay action for an expired unit, if any."""
    if unit.id is None or unit.valid_to is None:
        return None
    valid_to = _parse_datetime(unit.valid_to)
    if valid_to is None or valid_to >= datetime.now(UTC):
        return None
    if unit.unit_type in _REVIEW_HORIZON_TYPES:
        return ConsolidationIssue(unit.id, unit.title, "review horizon passed")
    return ConsolidationTransition(
        unit.id,
        unit.title,
        MemoryUnitStatus.ARCHIVED,
        f"valid_to passed: {unit.valid_to}",
    )


def _apply_llm_comparator(
    plan: ConsolidationPlan,
    eligible_units: list[MemoryUnitRecord],
    comparator: LLMComparator,
    threshold: float,
) -> ConsolidationPlan:
    """Drop low-scoring candidate transitions from a consolidation plan."""
    candidate_ids = {transition.unit_id for transition in _plan_transitions(plan)}
    candidates = [unit for unit in eligible_units if unit.id in candidate_ids]
    if not candidates:
        return plan
    scores = {
        score.unit_id: score.score
        for score in comparator.rescore_candidates(candidates, eligible_units)
    }

    def keep(transition: ConsolidationTransition) -> bool:
        score = scores.get(transition.unit_id)
        return score is None or score >= threshold

    return ConsolidationPlan(
        promotions=[transition for transition in plan.promotions if keep(transition)],
        candidates=[transition for transition in plan.candidates if keep(transition)],
        demotions=[transition for transition in plan.demotions if keep(transition)],
        conflicts=plan.conflicts,
        fts_pairs=plan.fts_pairs,
        supersessions=[transition for transition in plan.supersessions if keep(transition)],
        decay_archives=[transition for transition in plan.decay_archives if keep(transition)],
        decay_reviews=plan.decay_reviews,
        validator_rejections=plan.validator_rejections,
    )


def _plan_transitions(plan: ConsolidationPlan) -> list[ConsolidationTransition]:
    """Return all status transitions in deterministic plan order."""
    return [
        *plan.supersessions,
        *plan.decay_archives,
        *plan.promotions,
        *plan.candidates,
        *plan.demotions,
    ]


def _looks_conflicting(left: MemoryUnitRecord, right: MemoryUnitRecord) -> bool:
    left_text = f"{left.title}\n{left.content}".casefold()
    right_text = f"{right.title}\n{right.content}".casefold()
    combined = left_text + "\n" + right_text
    if any(term in combined for term in _STRONG_CORRECTION_TERMS):
        return False
    if len(claim_tokens(left_text) & claim_tokens(right_text)) < 3:
        return False
    return any(term in combined for term in _CONTRAST_TERMS)


def _looks_fts_pair(left: MemoryUnitRecord, right: MemoryUnitRecord) -> bool:
    """Return whether two units should be surfaced as FTS-like review pairs."""
    if left.unit_type != right.unit_type:
        return False
    left_text = f"{left.title}\n{left.content}".casefold()
    right_text = f"{right.title}\n{right.content}".casefold()
    combined = left_text + "\n" + right_text
    if any(term in combined for term in _STRONG_CORRECTION_TERMS):
        return False
    left_tokens = claim_tokens(left_text)
    right_tokens = claim_tokens(right_text)
    if len(left_tokens) < 4 or len(right_tokens) < 4:
        return False
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(overlap) >= 4 and (len(overlap) / len(union)) >= 0.35


def _looks_like_replacement(older: MemoryUnitRecord, newer: MemoryUnitRecord) -> bool:
    same_type = older.unit_type == newer.unit_type
    plan_to_decision = (
        older.unit_type == MemoryUnitType.PLAN and newer.unit_type == MemoryUnitType.DECISION
    )
    if not (same_type or plan_to_decision):
        return False
    older_text = f"{older.title}\n{older.content}".casefold()
    newer_text = f"{newer.title}\n{newer.content}".casefold()
    if len(claim_tokens(older_text).intersection(claim_tokens(newer_text))) < 2:
        return False
    if any(term in newer_text for term in _CORRECTION_TERMS):
        return True
    if "old" in newer_text and ("now" in newer_text or "updated" in newer_text):
        return True
    return "maybe" in older_text and newer.unit_type == MemoryUnitType.DECISION


def _source_refs(unit: MemoryUnitRecord) -> set[str]:
    return {f"{source.source_kind.value}:{source.source_ref}" for source in unit.sources}


def _unsupported_claim(unit: MemoryUnitRecord) -> bool:
    if any(source.source_kind == MemorySourceKind.MANUAL for source in unit.sources):
        return False
    content_tokens = claim_tokens(unit.content)
    if not content_tokens:
        return False
    source_quote = " ".join(source.quote for source in unit.sources).strip()
    if len(source_quote) < 20:
        return False
    source_tokens = claim_tokens(source_quote)
    if not source_tokens:
        return False
    return len(content_tokens.intersection(source_tokens)) < 2


def claim_tokens(text: str) -> set[str]:
    return {token for token in text.casefold().replace("_", " ").split() if len(token) >= 4}


def _parse_datetime(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
