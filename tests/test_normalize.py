"""Phase 3 tests: canonical predicate synonym dictionary."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# normalize_name (already covered in test_skeleton; added here for completeness)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alice", "alice"),
        ("  BOB  ", "bob"),
        ("multiple   spaces", "multiple spaces"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    from membox.normalize import normalize_name

    assert normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# normalize_predicate — canonical synonym map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,canonical",
    [
        # develop group
        ("develop", "develops"),
        ("developed", "develops"),
        ("developing", "develops"),
        ("creates", "develops"),
        ("create", "develops"),
        ("created", "develops"),
        ("authored", "develops"),
        ("wrote", "develops"),
        # use group
        ("use", "uses"),
        ("used", "uses"),
        ("using", "uses"),
        ("utilizes", "uses"),
        ("employs", "uses"),
        # build group
        ("build", "builds"),
        ("built", "builds"),
        ("building", "builds"),
        ("constructs", "builds"),
        # based_on group
        ("is based on", "based_on"),
        ("based on", "based_on"),
        ("relies on", "based_on"),
        ("built on", "based_on"),
        ("built upon", "based_on"),
        # integrate group
        ("integrate", "integrates"),
        ("integrated", "integrates"),
        # power group
        ("powered by", "powered_by"),
        # part_of group
        ("part of", "part_of"),
        ("is part of", "part_of"),
        ("belongs to", "part_of"),
        ("member of", "part_of"),
        # depends_on group
        ("depends on", "depends_on"),
        ("depend on", "depends_on"),
        ("requires", "depends_on"),
        # works_at group
        ("works at", "works_at"),
        ("worked at", "works_at"),
        ("employed by", "works_at"),
        # leads group
        ("leads", "leads"),
        ("led", "leads"),
        ("manages", "leads"),
        ("heads", "leads"),
        # Chinese variants
        ("开发", "develops"),
        ("使用", "uses"),
        ("用", "uses"),
        ("基于", "based_on"),
        ("集成", "integrates"),
        ("依赖", "depends_on"),
        ("构建", "builds"),
        ("属于", "part_of"),
        ("管理", "leads"),
        ("领导", "leads"),
    ],
)
def test_normalize_predicate_canonical(raw: str, canonical: str) -> None:
    from membox.normalize import normalize_predicate

    assert normalize_predicate(raw) == canonical


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Whitespace and case collapsing before lookup
        ("  DEVELOP  ", "develops"),
        ("  USES  ", "uses"),
        ("IS BASED ON", "based_on"),
        ("  Built   On  ", "based_on"),
        # Unknown predicates pass through normalized
        ("invented", "invented"),
        ("co-authored", "co-authored"),
        ("collaborates with", "collaborates with"),
        ("", ""),
    ],
)
def test_normalize_predicate_passthrough_and_whitespace(raw: str, expected: str) -> None:
    from membox.normalize import normalize_predicate

    assert normalize_predicate(raw) == expected


def test_normalize_predicate_idempotent_on_canonical_forms() -> None:
    """Canonical forms should map to themselves (or at least be stable on re-normalization)."""
    from membox.normalize import normalize_predicate

    canonical_forms = [
        "develops",
        "uses",
        "builds",
        "based_on",
        "integrates",
        "powers",
        "powered_by",
        "part_of",
        "depends_on",
        "works_at",
        "leads",
    ]
    for form in canonical_forms:
        result = normalize_predicate(form)
        # Either maps to itself or is stable (re-normalizing the result gives the same result)
        assert normalize_predicate(result) == result
