"""Pure workflow-distillation policy for lifecycle Phase F memory units."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from membox.core.consolidate import claim_tokens, has_explicit_user_confirmation
from membox.model.schema import MemoryUnitRecord, MemoryUnitType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

DISTILL_GROUP_MIN_OVERLAP = 0.35
"""Minimum token-overlap coefficient for grouping workflow memories."""

DISTILL_MIN_INDEPENDENT_EVIDENCE = 2
"""Independent-source count required for an automatic distill candidate."""

DISTILL_ASSET_MIN_OVERLAP = 0.40
"""Minimum title/name token-overlap coefficient for asset coverage."""

type DistillRecommendedForm = Literal["command", "script", "convention_doc", "skill_file"]

_SCRIPT_TERMS = {
    "check",
    "checks",
    "migration",
    "migrations",
    "script",
    "test",
    "tests",
    "verify",
}
_COMMAND_TERMS = {"cli", "command", "commands"}
_SKILL_TERMS = {"agent", "agents", "workflow", "workflows", "reuse", "reusable"}
_TARGET_EXTENSIONS = {".md", ".py", ".sh", ".bash", ".zsh", ".just"}
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?:\s|$)")
_TOKEN_SEPARATOR_RE = re.compile(r"[-./]+")


@dataclass(frozen=True)
class DistillMember:
    """One memory unit included in a distill candidate."""

    unit_id: int
    title: str
    unit_type: MemoryUnitType


@dataclass(frozen=True)
class AssetRecord:
    """One existing workflow asset discovered under a project root."""

    form: DistillRecommendedForm
    name: str
    path: str


class AssetInventory(Protocol):
    """Boundary for deterministic workflow-asset inventory."""

    def list_assets(self, root: Path) -> list[AssetRecord]:
        """Return workflow assets under ``root`` without parsing their behavior."""


@dataclass(frozen=True)
class DistillCandidate:
    """A repeated workflow worth reporting as a possible packageable asset."""

    members: list[DistillMember]
    evidence_sessions: int
    unit_count: int
    summed_recall_count: int
    recommended_form: DistillRecommendedForm
    explain: str
    covered_by: str | None = None


@dataclass(frozen=True)
class DistillPlan:
    """Read-only distillation report for one project/window."""

    candidates: list[DistillCandidate] = field(default_factory=list)
    scanned_unit_count: int = 0


class FilesystemAssetInventory:
    """Scan conventional local workflow asset locations by name and path only."""

    def list_assets(self, root: Path) -> list[AssetRecord]:
        """Return command, script, convention-doc, and skill assets under ``root``."""
        assets: list[AssetRecord] = []
        assets.extend(_file_assets(root / ".claude" / "commands", "command", "*.md", root))
        assets.extend(_file_assets(root / "scripts", "script", "*", root))
        assets.extend(_file_assets(root / "docs", "convention_doc", "*.md", root))
        assets.extend(_file_assets(root / "skills", "skill_file", "**/SKILL.md", root))
        assets.extend(_file_assets(root / "skills", "skill_file", "*.md", root))
        assets.extend(_makefile_assets(root / "Makefile"))
        assets.extend(_makefile_assets(root / "justfile"))
        return sorted(assets, key=lambda asset: (asset.form, asset.path))


def build_distill_plan(
    units: list[MemoryUnitRecord],
    independent_source_counts: dict[int, int],
    *,
    assets: list[AssetRecord] | None = None,
    independent_source_counter: Callable[[list[int]], int] | None = None,
) -> DistillPlan:
    """Build a deterministic read-only Phase F distillation report."""
    eligible = [unit for unit in units if unit.id is not None]
    groups = _group_units(eligible)
    candidates: list[DistillCandidate] = []
    for group in groups:
        unit_ids = [unit.id for unit in group if unit.id is not None]
        evidence_sessions = (
            independent_source_counter(unit_ids)
            if independent_source_counter is not None
            else sum(independent_source_counts.get(unit_id, 0) for unit_id in unit_ids)
        )
        explicit = any(has_explicit_user_confirmation(unit) for unit in group)
        if evidence_sessions < DISTILL_MIN_INDEPENDENT_EVIDENCE and not explicit:
            continue
        members = [
            DistillMember(unit.id or 0, unit.title, unit.unit_type)
            for unit in sorted(group, key=lambda item: item.id or 0)
        ]
        recommended_form = recommend_form(group)
        covered_by = _covered_by(group, recommended_form, assets or [])
        candidates.append(
            DistillCandidate(
                members=members,
                evidence_sessions=evidence_sessions,
                unit_count=len(group),
                summed_recall_count=sum(unit.recall_count for unit in group),
                recommended_form=recommended_form,
                covered_by=covered_by,
                explain=(
                    "distill candidate: "
                    f"evidence_sessions={evidence_sessions} "
                    f"unit_count={len(group)} explicit_user_approval={explicit}"
                ),
            )
        )
    return DistillPlan(
        candidates=sorted(
            candidates,
            key=lambda candidate: (
                -candidate.evidence_sessions,
                -candidate.unit_count,
                candidate.members[0].unit_id if candidate.members else 0,
            ),
        ),
        scanned_unit_count=len(eligible),
    )


def recommend_form(units: Iterable[MemoryUnitRecord]) -> DistillRecommendedForm:
    """Return the closest recommended-form value for a workflow group."""
    group = list(units)
    unit_types = {unit.unit_type for unit in group}
    tokens = set().union(*(_unit_tokens(unit) for unit in group)) if group else set()
    if MemoryUnitType.PROCEDURE in unit_types and tokens.intersection(_SCRIPT_TERMS):
        return "script"
    if MemoryUnitType.PROCEDURE in unit_types and tokens.intersection(_COMMAND_TERMS):
        return "command"
    if unit_types == {MemoryUnitType.LEARNING}:
        return "convention_doc"
    if tokens.intersection(_SKILL_TERMS) or len(unit_types) > 1:
        return "skill_file"
    if MemoryUnitType.PROCEDURE in unit_types:
        return "command"
    return "convention_doc"


def _group_units(units: list[MemoryUnitRecord]) -> list[list[MemoryUnitRecord]]:
    groups: list[list[MemoryUnitRecord]] = []
    for unit in sorted(units, key=lambda item: item.id or 0):
        matching = [g for g in groups if _overlaps_group(unit, g)]
        if matching:
            merged: list[MemoryUnitRecord] = [unit]
            for g in matching:
                merged.extend(g)
                groups.remove(g)
            groups.append(merged)
        else:
            groups.append([unit])
    return groups


def _overlaps_group(unit: MemoryUnitRecord, group: list[MemoryUnitRecord]) -> bool:
    unit_tokens = _unit_tokens(unit)
    return any(
        _token_overlap(unit_tokens, _unit_tokens(other)) >= DISTILL_GROUP_MIN_OVERLAP
        for other in group
    )


def _covered_by(
    group: list[MemoryUnitRecord],
    recommended_form: DistillRecommendedForm,
    assets: list[AssetRecord],
) -> str | None:
    title_tokens = set().union(*(_distill_tokens(unit.title) for unit in group))
    if not title_tokens:
        return None
    for asset in assets:
        if asset.form != recommended_form:
            continue
        if _token_overlap(title_tokens, _distill_tokens(asset.name)) >= DISTILL_ASSET_MIN_OVERLAP:
            return asset.path
    return None


def _unit_tokens(unit: MemoryUnitRecord) -> set[str]:
    return _distill_tokens(f"{unit.title}\n{unit.content}\n{unit.context}")


def _distill_tokens(text: str) -> set[str]:
    return claim_tokens(_TOKEN_SEPARATOR_RE.sub(" ", text))


def _token_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / min(len(left), len(right))


def _file_assets(
    directory: Path,
    form: DistillRecommendedForm,
    pattern: str,
    root: Path,
) -> list[AssetRecord]:
    if not directory.is_dir():
        return []
    assets: list[AssetRecord] = []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file() or path.suffix not in _TARGET_EXTENSIONS:
            continue
        name = path.parent.name if form == "skill_file" and path.name == "SKILL.md" else path.stem
        assets.append(AssetRecord(form, name, _relative_display(path, root)))
    return assets


def _makefile_assets(path: Path) -> list[AssetRecord]:
    if not path.is_file():
        return []
    assets: list[AssetRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _MAKE_TARGET_RE.match(line)
        if match is None or match.group(1).startswith("."):
            continue
        assets.append(AssetRecord("script", match.group(1), f"{path.name}:{match.group(1)}"))
    return assets


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
