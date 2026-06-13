"""SQLite FTS query helpers shared by store operation mixins."""

from __future__ import annotations

import re

# Intentionally covers only Han ideographs (U+3400-U+9FFF) for this slice;
# Hiragana, Katakana, and Hangul are out of scope.
CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
CJK_TRIGRAM_LIMIT = 64
CJK_ANCHOR_LIMIT = 96


def contains_cjk(text: str) -> bool:
    """Return whether *text* contains a CJK Unified Ideograph."""
    return bool(CJK_RUN_RE.search(text))


def cjk_trigram_terms(query: str, limit: int = CJK_TRIGRAM_LIMIT) -> list[str]:
    """Return ordered unique 3-character CJK windows for trigram FTS MATCH."""
    return cjk_anchor_terms(query, min_len=3, max_len=3, limit=limit)


def cjk_anchor_terms(
    query: str,
    min_len: int = 2,
    max_len: int = 4,
    limit: int = CJK_ANCHOR_LIMIT,
) -> list[str]:
    """Return ordered unique CJK n-grams for reranking and excerpt anchoring."""
    terms: list[str] = []
    seen: set[str] = set()
    for run_match in CJK_RUN_RE.finditer(query):
        run = run_match.group(0)
        for size in range(max_len, min_len - 1, -1):
            if len(run) < size:
                continue
            for idx in range(len(run) - size + 1):
                term = run[idx : idx + size]
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
                    if len(terms) >= limit:
                        return terms
    return terms


def fts5_or_query(query: str) -> str:
    """Build an OR-of-tokens FTS5 MATCH expression from a natural-language query."""
    cleaned = re.sub(r'["\*\^\(\)\{\}\[\]:,?!。?!、;;\.]', " ", query)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def fts5_query_from_terms(terms: list[str]) -> str:
    """Build a quoted OR MATCH expression from pre-sanitized terms."""
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)


def fts_table_exists(conn: object, table_name: str) -> bool:
    """Return whether an FTS table exists in the current SQLite database."""
    return bool(
        conn.execute(  # type: ignore[attr-defined]
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        ).fetchone()
    )
