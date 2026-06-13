"""JSON lease helpers for meta-table ownership records."""

from __future__ import annotations

import datetime
import json
import os
import socket


def utcnow() -> datetime.datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.datetime.now(tz=datetime.UTC)


def render_lease() -> str:
    """Serialize a lease record for the current process as JSON."""
    return json.dumps(
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "heartbeat": utcnow().isoformat(),
        }
    )


def parse_lease(value: str) -> dict[str, object] | None:
    """Parse a lease JSON string; return None when malformed."""
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def lease_is_live(lease: dict[str, object], ttl: float) -> bool:
    """Return True when the lease heartbeat is younger than *ttl* seconds."""
    raw = lease.get("heartbeat")
    if not isinstance(raw, str):
        return False
    try:
        heartbeat = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=datetime.UTC)
    return (utcnow() - heartbeat).total_seconds() < ttl


def lease_is_mine(lease: dict[str, object]) -> bool:
    """Return True when the lease is owned by the current process."""
    return lease.get("pid") == os.getpid() and lease.get("hostname") == socket.gethostname()
