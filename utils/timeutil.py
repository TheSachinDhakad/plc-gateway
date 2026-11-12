"""UTC time helpers and UUIDv7 generation (time-ordered unique ids for idempotent inserts)."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime

_uuid_lock = threading.Lock()
_last_ms = 0
_counter = 0


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as tz-aware UTC. Naive datetimes are interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_iso(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value is not None else None


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def to_mysql(value: datetime) -> datetime:
    """MySQL DATETIME has no timezone: store naive UTC."""
    return ensure_utc(value).replace(tzinfo=None)


def from_mysql(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC)


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit unix ms timestamp, monotonic 12-bit counter, 62 random bits."""
    global _last_ms, _counter
    with _uuid_lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            _counter += 1
            if _counter > 0xFFF:
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        else:
            _last_ms = ms
            _counter = 0
        counter = _counter
    rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand
    return uuid.UUID(int=value)
