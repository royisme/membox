"""Categorize memory units for MEMORY.md-style export."""

from __future__ import annotations

from membox.core.consolidate import _unsupported_claim
from membox.model.schema import MemoryUnitRecord, MemoryUnitType

SECTION_GOTCHAS = "Gotchas"
SECTION_ARCHITECTURE = "Architecture decisions"
SECTION_RULES = "Rules / Conventions"
SECTION_KNOWLEDGE = "Discovered durable knowledge"
SECTION_OTHER = "Other"

EXPORT_SECTIONS: tuple[str, ...] = (
    SECTION_GOTCHAS,
    SECTION_ARCHITECTURE,
    SECTION_RULES,
    SECTION_KNOWLEDGE,
    SECTION_OTHER,
)
"""Deterministic section precedence for MEMORY.md export.

First match wins — see ``categorize_for_export`` for the per-unit routing.
"""


def categorize_for_export(
    units: list[MemoryUnitRecord],
) -> dict[str, list[MemoryUnitRecord]]:
    """Group units into the four MEMORY.md sections plus an Other bucket.

    Each unit maps to exactly one section, ordered here by precedence:

    1. ``Gotchas`` — units flagged ``security``/``performance`` OR whose
       content cannot be tied back to source quotes (unsupported claim).
    2. ``Architecture decisions`` — ``unit_type == DECISION``.
    3. ``Rules / Conventions`` — ``unit_type`` in ``{PREFERENCE, PROCEDURE}``,
       or any unit labelled ``conventions``.
    4. ``Discovered durable knowledge`` — ``unit_type`` in ``{FACT, LEARNING}``.
    5. ``Other`` — leftover ``CONTEXT``/``PLAN``/``EVENT`` units.

    Returns a dict keyed by section name; missing sections simply aren't
    included. The caller is responsible for rendering order.
    """
    grouped: dict[str, list[MemoryUnitRecord]] = {section: [] for section in EXPORT_SECTIONS}
    for unit in units:
        labels = set(unit.labels)
        if ("security" in labels or "performance" in labels) or _unsupported_claim(unit):
            grouped[SECTION_GOTCHAS].append(unit)
            continue
        if unit.unit_type == MemoryUnitType.DECISION:
            grouped[SECTION_ARCHITECTURE].append(unit)
            continue
        if (
            unit.unit_type in {MemoryUnitType.PREFERENCE, MemoryUnitType.PROCEDURE}
            or "conventions" in labels
        ):
            grouped[SECTION_RULES].append(unit)
            continue
        if unit.unit_type in {MemoryUnitType.FACT, MemoryUnitType.LEARNING}:
            grouped[SECTION_KNOWLEDGE].append(unit)
            continue
        grouped[SECTION_OTHER].append(unit)
    return {section: items for section, items in grouped.items() if items}
