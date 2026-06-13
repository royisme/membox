"""Vector encoding and similarity helpers shared by store operation mixins."""

from __future__ import annotations

import math
import struct


def vec_to_blob(vector: list[float]) -> bytes:
    """Pack a float vector into the SQLite BLOB encoding used by membox."""
    return struct.pack(f"{len(vector)}f", *vector)


def blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a SQLite BLOB vector encoded by :func:`vec_to_blob`."""
    length = len(blob) // 4
    return list(struct.unpack(f"{length}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length vectors.

    Raises:
        ValueError: If the vectors have different dimensions.
    """
    if len(a) != len(b):
        msg = f"Vector dimension mismatch: {len(a)} != {len(b)}"
        raise ValueError(msg)
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
