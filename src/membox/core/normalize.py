"""membox normalize — predicate and name normalization utilities."""

from __future__ import annotations

# Canonical predicate map. Extend as needed; intentionally small and explicit
# so behavior stays predictable. Keys are lowercased surface forms; values are
# the single canonical form stored in the database.
_PREDICATE_CANONICAL: dict[str, str] = {
    # --- develop / create ---
    "develop": "develops",
    "developed": "develops",
    "developing": "develops",
    "creates": "develops",
    "create": "develops",
    "created": "develops",
    "creating": "develops",
    "authored": "develops",
    "authors": "develops",
    "wrote": "develops",
    "writes": "develops",
    # --- use ---
    "use": "uses",
    "used": "uses",
    "using": "uses",
    "utilizes": "uses",
    "utilize": "uses",
    "utilized": "uses",
    "employs": "uses",
    "employ": "uses",
    # --- build ---
    "build": "builds",
    "built": "builds",
    "building": "builds",
    "constructs": "builds",
    "construct": "builds",
    "constructed": "builds",
    # --- based on ---
    "is based on": "based_on",
    "based on": "based_on",
    "relies on": "based_on",
    "rely on": "based_on",
    "relies upon": "based_on",
    "built on": "based_on",
    "built upon": "based_on",
    # --- integrate ---
    "integrate": "integrates",
    "integrated": "integrates",
    "integrating": "integrates",
    # --- power ---
    "powers": "powers",
    "powered by": "powered_by",
    "power": "powers",
    # --- part of ---
    "part of": "part_of",
    "is part of": "part_of",
    "belongs to": "part_of",
    "belongs_to": "part_of",
    "member of": "part_of",
    "component of": "part_of",
    # --- depend on ---
    "depends on": "depends_on",
    "depend on": "depends_on",
    "dependent on": "depends_on",
    "requires": "depends_on",
    "require": "depends_on",
    # --- work at ---
    "works at": "works_at",
    "work at": "works_at",
    "worked at": "works_at",
    "employed by": "works_at",
    "employed at": "works_at",
    # --- lead ---
    "leads": "leads",
    "lead": "leads",
    "led": "leads",
    "manages": "leads",
    "manage": "leads",
    "managed": "leads",
    "heads": "leads",
    "head": "leads",
    # --- Chinese variants ---
    "开发": "develops",
    "创建": "develops",
    "创造": "develops",
    "使用": "uses",
    "用": "uses",
    "利用": "uses",
    "基于": "based_on",
    "依赖": "depends_on",
    "集成": "integrates",
    "整合": "integrates",
    "构建": "builds",
    "搭建": "builds",
    "属于": "part_of",
    "是…的一部分": "part_of",
    "管理": "leads",
    "领导": "leads",
}


def normalize_name(name: str) -> str:
    """Lowercase and collapse whitespace for deterministic canonicalization.

    Args:
        name: Raw entity name string.

    Returns:
        Lowercased, whitespace-collapsed string.
    """
    return " ".join(name.strip().lower().split())


def normalize_predicate(predicate: str) -> str:
    """Normalize a predicate to its canonical form via the synonym dictionary.

    Lowercases and collapses whitespace first, then looks up the canonical
    form. Returns the input unchanged if no mapping exists.

    Args:
        predicate: Raw predicate string from extraction.

    Returns:
        Canonical predicate string (e.g. "developed" → "develops").
    """
    p = " ".join(predicate.strip().lower().split())
    return _PREDICATE_CANONICAL.get(p, p)
