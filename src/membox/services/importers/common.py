"""Shared helpers for history importers: JSONL iteration and stable IDs."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def iter_jsonl(path: Path, offset_bytes: int = 0) -> Iterator[tuple[dict[str, object], int, int]]:
    """Yield JSON records from a ``.jsonl`` file with byte offsets.

    Lines that are blank or fail to parse are skipped silently — upstream
    logs may end with a partially written line while a session is live; the
    returned offsets only ever advance past fully parsed lines, so a resume
    re-reads the partial tail.

    Args:
        path: JSONL file to read.
        offset_bytes: Byte offset to start from (0 = beginning).

    Yields:
        ``(record, offset_before, offset_after)`` triples, where the offsets
        bracket the line the record came from.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    with path.open("rb") as fh:
        fh.seek(offset_bytes)
        pos = offset_bytes
        for raw in fh:
            before = pos
            pos += len(raw)
            if not raw.endswith(b"\n"):
                # Partial trailing line (log still being written): leave it
                # for the next import pass.
                break
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record, before, pos


def count_malformed_jsonl_lines(path: Path, offset_bytes: int = 0) -> int:
    """Count complete non-blank JSONL lines skipped as malformed."""
    skipped = 0
    with path.open("rb") as fh:
        fh.seek(offset_bytes)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
    return skipped


def opt_str(value: object) -> str | None:
    """Return *value* as a string, or None when absent or non-string."""
    return value if isinstance(value, str) else None


def synth_external_id(role: str, created_at: object, text: str, seen: set[str]) -> str:
    """Synthesize a stable external ID for a record the upstream left unkeyed.

    The ID is a short content hash of role, timestamp, and text head — stable
    across re-imports and independent of file position.  ``seen`` deduplicates
    pathological identical records within one parse pass.

    Args:
        role: Record role (or kind) string.
        created_at: Upstream timestamp, or None.
        text: Record text (only the first 256 chars contribute).
        seen: External IDs already assigned in this parse pass.

    Returns:
        A hex digest, suffixed ``~2``, ``~3``, … on collision.
    """
    basis = f"{role}\x00{created_at}\x00{text[:256]}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    candidate = digest
    bump = 1
    while candidate in seen:
        bump += 1
        candidate = f"{digest}~{bump}"
    return candidate
