"""Drift-free, overrun-aware scan-class scheduling (pure logic, injectable clock).

Each device has one scan class per distinct ``poll_interval_ms``. Deadlines advance by the
interval from the *previous deadline* (no drift). If a cycle takes longer than its interval,
missed deadlines are skipped (counted as overruns) instead of bursting to catch up.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from models.tag import TagConfig


@dataclass(slots=True)
class ScanClass:
    interval_ms: int
    tags: list[TagConfig]
    next_due: float
    cycles: int = 0
    overruns: int = 0
    last_duration_ms: float = 0.0
    max_duration_ms: float = 0.0
    last_started: float | None = field(default=None)

    @property
    def interval_s(self) -> float:
        return self.interval_ms / 1000.0


class PollingScheduler:
    def __init__(
        self,
        scan_classes: dict[int, list[TagConfig]],
        *,
        clock: Callable[[], float] = time.monotonic,
        start_offset_s: float = 0.0,
    ) -> None:
        self._clock = clock
        now = clock() + start_offset_s
        self.scan_classes = [ScanClass(interval, list(tags), now) for interval, tags in sorted(scan_classes.items())
                             if tags]

    def next_due(self) -> tuple[list[ScanClass], float]:
        """Return (scan classes due now, seconds until the earliest next deadline)."""
        if not self.scan_classes:
            return [], 3600.0
        now = self._clock()
        due = [sc for sc in self.scan_classes if sc.next_due <= now]
        if due:
            return due, 0.0
        return [], max(0.0, min(sc.next_due for sc in self.scan_classes) - now)

    def mark_started(self, sc: ScanClass) -> None:
        sc.last_started = self._clock()

    def mark_done(self, sc: ScanClass) -> int:
        """Advance the deadline; return number of skipped (overrun) cycles."""
        now = self._clock()
        if sc.last_started is not None:
            sc.last_duration_ms = (now - sc.last_started) * 1000
            sc.max_duration_ms = max(sc.max_duration_ms, sc.last_duration_ms)
        sc.cycles += 1
        sc.next_due += sc.interval_s
        skipped = 0
        if sc.next_due <= now:
            skipped = math.floor((now - sc.next_due) / sc.interval_s) + 1
            sc.next_due += skipped * sc.interval_s
            sc.overruns += skipped
        return skipped

    def reset(self) -> None:
        """Re-align all deadlines to now (after reconnect)."""
        now = self._clock()
        for sc in self.scan_classes:
            sc.next_due = now
