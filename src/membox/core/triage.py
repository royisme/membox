"""membox triage — pure domain logic for the memory lifecycle gates.

Phase B ships only the secret-redaction scrubber; the heuristic triage gate
and its keyword tables arrive with Phase C.  Per the lifecycle design, the
pattern table is a small reviewed list living in code, redaction applies to
everything Membox stores (previews and FTS), and it is on by default and not
silently disablable per import.  No I/O happens in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from membox.model.schema import MEMORY_LABELS, MemoryTemporalType, MemoryUnitType, MemoryUserIntent

GATE_VERSION = "heuristic-v1"
"""Version string persisted with every deterministic triage decision."""

REDACTION_MARKER = "[REDACTED]"
"""Replacement text for every secret match."""

# Reviewed pattern list.  Order matters only for readability; all patterns are
# applied.  Each entry redacts the secret value, keeping surrounding context
# searchable (e.g. "OPENAI_API_KEY=[REDACTED]").
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Well-known token prefixes (OpenAI, Anthropic, GitHub, Slack, Stripe,
    # AWS access keys, Google API keys, JWTs).
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # KEY=/TOKEN=/SECRET=/PASSWORD= style assignments (env dumps, .env files).
    # The name part is kept; only the value is redacted.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)[A-Z0-9_]*"
        r"\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}\2"
    ),
    # PEM private-key blocks (multi-line).
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Bearer tokens in HTTP headers.
    re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
]

UNIT_TYPE_PRIORITY: tuple[MemoryUnitType, ...] = (
    MemoryUnitType.PREFERENCE,
    MemoryUnitType.DECISION,
    MemoryUnitType.PROCEDURE,
    MemoryUnitType.FACT,
    MemoryUnitType.LEARNING,
    MemoryUnitType.PLAN,
    MemoryUnitType.EVENT,
    MemoryUnitType.CONTEXT,
)
"""Tie-break priority for the closed Phase C unit taxonomy."""

_EXPLICIT_MEMORY = (
    "remember",
    "always",
    "never",
    "rule",
    "decision",
    "we decided",
    "use this going forward",
    "记住",
    "以后",
    "规则",
    "决定",
)
_DURABLE_CHANGE = (
    "architecture",
    "schema",
    "migration",
    "api contract",
    "cli",
    "storage",
    "retrieval",
    "validation gate",
    "架构",
    "迁移",
    "存储",
    "检索",
)
_FIX_SIGNALS = ("error", "failed", "failure", "bug", "fixed", "resolved", "修复", "报错")
_PROCEDURE_SIGNALS = ("step", "first", "then", "run `", "command", "步骤", "先", "然后")
_CORRECTION_SIGNALS = ("correction", "actually", "instead", "不是", "纠正", "改成")
_PLAN_SIGNALS = ("plan", "todo", "next", "roadmap", "计划", "下一步")

_LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "architecture": ("architecture", "schema", "migration", "架构", "迁移"),
    "storage": ("storage", "sqlite", "database", "存储"),
    "retrieval": ("retrieval", "fts", "query", "检索"),
    "cli": ("cli", "command", "membox ", "命令"),
    "testing": ("test", "pytest", "eval", "gate", "测试", "验收"),
    "tooling": ("tool", "ruff", "mypy", "uv ", "工具"),
    "workflow": ("workflow", "process", "steps", "流程"),
    "conventions": ("convention", "rule", "style", "约定", "规则"),
    "dependencies": ("dependency", "package", "依赖"),
    "performance": ("performance", "cache", "batch", "latency", "性能"),
    "security": ("secret", "token", "redact", "安全"),
}


@dataclass(frozen=True)
class GateDecision:
    """Deterministic triage decision for one trace item."""

    should_extract: bool
    unit_type: MemoryUnitType
    importance_score: float
    confidence_score: float
    temporal_type: MemoryTemporalType
    user_intent: MemoryUserIntent
    extraction_hint: str
    reason: str
    labels: list[str] = field(default_factory=list)
    gate_version: str = GATE_VERSION


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings with :data:`REDACTION_MARKER`.

    Applied by the history store layer to message ``text`` and event ``body``
    before any persistence or FTS indexing, so secrets never become
    searchable.  ``history fetch`` re-reads the user's own upstream file and
    is intentionally outside this boundary.

    Args:
        text: Raw text from an imported session log.

    Returns:
        Text with every pattern match replaced.  Assignment-style matches
        keep the variable name and redact only the value.
    """
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(rf"\g<1>{REDACTION_MARKER}", text)
        else:
            text = pattern.sub(REDACTION_MARKER, text)
    return text


def triage_trace(
    text: str,
    *,
    role: str = "",
    user_intent: MemoryUserIntent = MemoryUserIntent.AUTO,
) -> GateDecision:
    """Run the Phase C deterministic heuristic gate over one bounded trace item."""
    window = text[:4000]
    lowered = window.casefold()
    manual = user_intent == MemoryUserIntent.MANUAL
    explicit = _has_any(lowered, _EXPLICIT_MEMORY)
    durable = _has_any(lowered, _DURABLE_CHANGE)
    fix = _has_any(lowered, _FIX_SIGNALS) and _has_any(
        lowered, ("fixed", "resolved", "pass", "green", "修复", "解决")
    )
    procedure = _has_any(lowered, _PROCEDURE_SIGNALS)
    correction = _has_any(lowered, _CORRECTION_SIGNALS)
    role_weighted_explicit = explicit and role in {"user", "developer", "system"}

    if manual:
        importance, confidence, reason = 0.90, 0.85, "manual_intent"
    elif role_weighted_explicit or correction:
        importance, confidence, reason = 0.80, 0.75, "explicit_decision_or_rule"
    elif fix or procedure:
        importance, confidence, reason = 0.65, 0.65, "failure_fix_or_procedure"
    elif durable and explicit:
        importance, confidence, reason = 0.65, 0.60, "durable_change_with_intent"
    else:
        importance, confidence, reason = 0.35, 0.50, "weak_context_only"

    unit_type = _infer_unit_type(lowered, correction=correction, fix=fix, procedure=procedure)
    labels = _infer_labels(lowered)
    should_extract = reason != "weak_context_only"
    hint = _first_line(window)
    return GateDecision(
        should_extract=should_extract,
        unit_type=unit_type,
        importance_score=importance,
        confidence_score=min(confidence, 0.85),
        temporal_type=_infer_temporal(lowered),
        user_intent=user_intent,
        extraction_hint=hint[:200],
        reason=reason,
        labels=labels,
    )


def activation_passes(decision: GateDecision, *, has_source: bool) -> bool:
    """Return whether a triaged candidate should become an active unit."""
    return (
        has_source
        and decision.unit_type in UNIT_TYPE_PRIORITY
        and set(decision.labels).issubset(MEMORY_LABELS)
        and decision.confidence_score >= 0.50
        and (decision.importance_score >= 0.45 or decision.user_intent == MemoryUserIntent.MANUAL)
    )


def _infer_unit_type(
    lowered: str,
    *,
    correction: bool,
    fix: bool,
    procedure: bool,
) -> MemoryUnitType:
    if "preference" in lowered or "always" in lowered or "never" in lowered:
        return MemoryUnitType.PREFERENCE
    if "decision" in lowered or "we decided" in lowered or "决定" in lowered:
        return MemoryUnitType.DECISION
    if procedure:
        return MemoryUnitType.PROCEDURE
    if fix:
        return MemoryUnitType.LEARNING
    if _has_any(lowered, _PLAN_SIGNALS):
        return MemoryUnitType.PLAN
    if correction:
        return MemoryUnitType.FACT
    if _has_any(lowered, _DURABLE_CHANGE):
        return MemoryUnitType.FACT
    return MemoryUnitType.CONTEXT


def _infer_temporal(lowered: str) -> MemoryTemporalType:
    if "always" in lowered or "never" in lowered or "以后" in lowered:
        return MemoryTemporalType.ONGOING
    if "next" in lowered or "plan" in lowered or "下一步" in lowered:
        return MemoryTemporalType.RANGE
    return MemoryTemporalType.UNKNOWN


def _infer_labels(lowered: str) -> list[str]:
    labels = [
        label
        for label, keywords in _LABEL_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    ]
    return labels or ["workflow"]


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text.strip()
