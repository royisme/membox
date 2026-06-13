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

CRYSTAL_MAX_CONTENT_LENGTH = 1500
"""Crystal-specific content cap (half of MAX_CONTENT_LENGTH).

Crystal units are the durable set recalled repeatedly; keeping them compact
(<=1500 chars) ensures each fits cleanly into recall context without burning
the per-query budget. Distinct from MAX_CONTENT_LENGTH (4000) which guards the
general unit cap.
"""

MIN_MEANINGFUL_TOKENS = 3
"""Vague-content floor for crystal promotion.

A claim must carry at least 3 meaningful (>=4-letter) tokens, else it is flagged
as vague. This is deliberately conservative: the heuristic must catch true noise
("ok thanks", "see above", "done") — 0-1 meaningful tokens — without rejecting
legitimately *concise* substantive claims. Crisp agent-extracted decisions such
as "Adopt WAL mode for all connections" (3 meaningful tokens) are real durable
knowledge, not vagueness, and must pass. A higher floor over-rejects the concise
units the agent-as-provider flow produces by design.
"""

_VAGUE_CONTENT_LENGTH_CEILING = 80
"""Maximum raw content length to apply the vague-content heuristic.

Conservative ceiling: only short claims (under 80 chars) are evaluated for
"thin Latin token" vagueness. Substantial content (e.g. CJK-heavy but long
claims) is not flagged, since the heuristic is Latin-token-based and would
otherwise misclassify legitimate long-form claims in other scripts.
"""

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
    validator_flags: list[ConsolidationIssue] = field(default_factory=list)

    def ordered_transitions(self) -> list[ConsolidationTransition]:
        """Return status transitions in the deterministic apply order."""
        return [
            *self.supersessions,
            *self.decay_archives,
            *self.promotions,
            *self.candidates,
            *self.demotions,
        ]

    def transition_groups(self) -> tuple[tuple[str, list[ConsolidationTransition]], ...]:
        """Return labelled transition groups in render/apply order."""
        return (
            ("supersede", self.supersessions),
            ("archive", self.decay_archives),
            ("promote", self.promotions),
            ("candidate", self.candidates),
            ("demote", self.demotions),
        )

    def review_pairs(self) -> list[ConsolidationConflict]:
        """Return deterministic conflicts and FTS review pairs for presentation."""
        return [*self.conflicts, *self.fts_pairs]


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
    validator_rejections, validator_flags = validate_units(units)
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
        validator_flags=validator_flags,
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


def validate_units(
    units: list[MemoryUnitRecord],
) -> tuple[list[ConsolidationIssue], list[ConsolidationIssue]]:
    """Run the Phase D validator over units without mutating them.

    Returns:
        ``(rejections, flags)``: hard rejections block promotion, advisory
        flags surface without blocking.  Hard rules (no source, content
        length, vague content, crystal budget, weak provenance, duplicate
        titles, stale paths, unsupported claims) always reject.

        The M4 Part A2 why/how/next rationale rules are GATED for
        agent/LLM-extracted units (any source whose source_kind is *not*
        HISTORY_MESSAGE / HISTORY_EVENT — i.e. the agent wrote them with
        full semantic context) and FLAGGED (advisory) for heuristic
        units whose why/how/next are absent because the deterministic
        checkpoint path never sets them.
    """
    rejections: list[ConsolidationIssue] = []
    flags: list[ConsolidationIssue] = []
    seen_titles: dict[tuple[str, str], int] = {}
    for unit in units:
        if unit.id is None:
            continue
        if not unit.sources:
            rejections.append(ConsolidationIssue(unit.id, unit.title, "no source"))
        content_len = len(unit.content.strip())
        content_in_bounds = MIN_CONTENT_LENGTH <= content_len <= MAX_CONTENT_LENGTH
        if not content_in_bounds:
            rejections.append(
                ConsolidationIssue(unit.id, unit.title, "content length out of bounds")
            )
        # Vague-content heuristic: only meaningful when content is in-bounds
        # (length check already handled the empty/oversized case). Flag units
        # whose content is too thin to carry durable signal — either fewer than
        # MIN_MEANINGFUL_TOKENS 4-letter tokens, or content that essentially
        # restates the title (strict fewer tokens than the title and <12 total).
        # Skip entirely when content is non-Latin-heavy (CJK or other scripts):
        # the Latin-tokenizer can't evaluate "meaningfulness" on scripts it
        # doesn't recognize, and we'd over-flag legitimate CJK claims.
        if content_in_bounds:
            meaningful = claim_tokens(unit.content)
            if (
                meaningful
                and content_len < _VAGUE_CONTENT_LENGTH_CEILING
                and not _has_non_latin_content(unit.content)
            ):
                title_tokens = claim_tokens(unit.title)
                too_few = len(meaningful) < MIN_MEANINGFUL_TOKENS
                # Title-echo: content has FEWER tokens than the title AND
                # the total is small. A strict-less-than comparison avoids
                # false positives where content and title were derived from
                # the same source text and happen to share identical token
                # counts. The heuristic still catches true restatements
                # ("ok thanks" / title="Thanks" → 2 content tokens, 1 title).
                title_echo = (
                    len(title_tokens) > 0
                    and len(meaningful) < len(title_tokens)
                    and len(meaningful) < 12
                )
                if too_few or title_echo:
                    rejections.append(
                        ConsolidationIssue(
                            unit.id, unit.title, "vague content (insufficient signal)"
                        )
                    )
        if content_in_bounds and content_len > CRYSTAL_MAX_CONTENT_LENGTH:
            rejections.append(
                ConsolidationIssue(
                    unit.id,
                    unit.title,
                    f"content exceeds crystal budget ({content_len} > {CRYSTAL_MAX_CONTENT_LENGTH} chars)",
                )
            )
        if unit.sources and not _has_strong_provenance(unit):
            rejections.append(
                ConsolidationIssue(
                    unit.id,
                    unit.title,
                    "insufficient provenance for crystal (needs a quote or ≥2 sources)",
                )
            )
        title_key = (unit.project, unit.title.strip().casefold())
        previous = seen_titles.get(title_key)
        if previous is not None:
            rejections.append(
                ConsolidationIssue(unit.id, unit.title, f"duplicate title also used by {previous}")
            )
        else:
            seen_titles[title_key] = unit.id
        # Stale-path check now applies to ANY source whose source_ref is an
        # absolute filesystem path (DOCUMENT and beyond). The absolute-path
        # guard ensures non-path refs like "manual:1" or
        # "history_message:membox-capture:..." are not misclassified.
        seen_stale_refs: set[str] = set()
        for source in unit.sources:
            ref = source.source_ref
            if not ref or ref in seen_stale_refs:
                continue
            if Path(ref).is_absolute() and not Path(ref).exists():
                rejections.append(
                    ConsolidationIssue(unit.id, unit.title, f"stale source path: {ref}")
                )
                seen_stale_refs.add(ref)
        if unit.sources and _unsupported_claim(unit):
            rejections.append(
                ConsolidationIssue(unit.id, unit.title, "unsupported claim heuristic")
            )
        # M4 Part A2 rationale rules.  Agent/LLM-extracted units are GATED
        # (rejected from promotion); heuristic units are FLAGGED (advisory,
        # do not block).  See _is_agent_extracted.
        for issue in _rationale_issues(unit):
            if _is_agent_extracted(unit):
                rejections.append(issue)
            else:
                flags.append(issue)
    return rejections, flags


def _is_agent_extracted(unit: MemoryUnitRecord) -> bool:
    """Return whether *unit* was produced by an agent/LLM (not the heuristic).

    Heuristic units carry HISTORY_MESSAGE / HISTORY_EVENT source kinds
    (the deterministic checkpoint path in
    :func:`cli.commands.memory._unit_from_trace`); every other kind
    (DOCUMENT, RELATION, UNIT, MANUAL) is treated as agent/LLM-extracted
    and is therefore subject to the LIVE rationale gate.
    """
    heuristic_kinds = {
        MemorySourceKind.HISTORY_MESSAGE.value,
        MemorySourceKind.HISTORY_EVENT.value,
    }
    return not all(source.source_kind.value in heuristic_kinds for source in unit.sources)


def _rationale_issues(unit: MemoryUnitRecord) -> list[ConsolidationIssue]:
    """Yield rationale-rule issues for one unit (empty list when satisfied)."""
    if unit.id is None:
        return []
    issues: list[ConsolidationIssue] = []
    needs_why = unit.unit_type in {
        MemoryUnitType.DECISION,
        MemoryUnitType.LEARNING,
        MemoryUnitType.PROCEDURE,
    }
    if needs_why and not _has_text(unit.why):
        issues.append(ConsolidationIssue(unit.id, unit.title, "decision missing rationale (why)"))
    if unit.unit_type in {MemoryUnitType.PROCEDURE, MemoryUnitType.PLAN} and not _has_text(
        unit.next_step
    ):
        issues.append(ConsolidationIssue(unit.id, unit.title, "procedure/plan missing next_step"))
    if unit.unit_type == MemoryUnitType.PROCEDURE and not _has_text(unit.how_to_apply):
        issues.append(ConsolidationIssue(unit.id, unit.title, "procedure missing how_to_apply"))
    return issues


def _has_text(value: str | None) -> bool:
    """Return whether *value* is a non-empty / non-whitespace string."""
    if value is None:
        return False
    return bool(value.strip())


def _has_non_latin_content(text: str) -> bool:
    """Return whether *text* contains any non-Latin characters.

    Used to gate the vague-content heuristic so CJK or other-script content
    isn't evaluated against a Latin-only tokenizer. Any single non-Latin
    character is enough — the heuristic is Latin-token based and would
    otherwise over-flag legitimate multilingual claims.
    """
    if not text:
        return False
    return any(ord(char) > 0x02FF for char in text)


def _has_strong_provenance(unit: MemoryUnitRecord) -> bool:
    """Return whether a unit has enough evidence to deserve crystal promotion.

    Strong provenance requires EITHER one source carrying a non-empty quote
    (i.e. the agent saw the actual claim text), OR at least two distinct
    source_refs (independent observations). A single thin source — no quote,
    one ref — is treated as too weak to anchor a durable crystal.
    """
    if any((s.quote or "").strip() for s in unit.sources):
        return True
    distinct_refs = {s.source_ref for s in unit.sources if s.source_ref}
    return len(distinct_refs) >= 2


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
    candidate_ids = {transition.unit_id for transition in plan.ordered_transitions()}
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
        validator_flags=plan.validator_flags,
    )


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
