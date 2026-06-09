"""membox normalize — predicate and name normalization utilities. Phase 1: lowercase stub."""

from __future__ import annotations


def normalize_name(name: str) -> str:
    """Lowercase and collapse whitespace for deterministic canonicalization.

    Args:
        name: Raw entity name string.

    Returns:
        Lowercased, whitespace-collapsed string.
    """
    return " ".join(name.strip().lower().split())


def normalize_predicate(predicate: str) -> str:
    """Normalize a predicate to its canonical form.

    Phase 1 stub: lowercases and collapses whitespace only.
    Phase 3 will add the full canonical synonym dictionary.

    Args:
        predicate: Raw predicate string from extraction.

    Returns:
        Normalized predicate string.
    """
    return " ".join(predicate.strip().lower().split())
