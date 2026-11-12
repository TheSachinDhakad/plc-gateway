"""In-process metrics: labelled counters, gauges and rolling duration statistics."""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Any


def _key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    return name + "{" + ",".join(f"{k}={labels[k]}" for k in sorted(labels)) + "}"


class DurationStat:
    __slots__ = ("_values", "count", "last", "total")

    def __init__(self, window: int = 1000) -> None:
        self._values: deque[float] = deque(maxlen=window)
        self.count = 0
        self.total = 0.0
        self.last = 0.0

    def add(self, value_ms: float) -> None:
        self._values.append(value_ms)
        self.count += 1
        self.total += value_ms
        self.last = value_ms

    def snapshot(self) -> dict[str, float]:
        if not self._values:
            return {"count": self.count, "last": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        values = sorted(self._values)
        p95_index = max(0, min(len(values) - 1, round(0.95 * (len(values) - 1))))
        return {
            "count": self.count,
            "last": round(self.last, 3),
            "p50": round(statistics.median(values), 3),
            "p95": round(values[p95_index], 3),
            "max": round(values[-1], 3),
        }


class Metrics:
    """Thread-safe (blocking drivers report from executor threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._durations: dict[str, DurationStat] = {}
        self.started_monotonic = time.monotonic()

    def inc(self, name: str, value: int = 1, **labels: Any) -> None:
        key = _key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value_ms: float, **labels: Any) -> None:
        key = _key(name, labels)
        with self._lock:
            stat = self._durations.get(key)
            if stat is None:
                stat = self._durations[key] = DurationStat()
            stat.add(value_ms)

    def counter(self, name: str, **labels: Any) -> int:
        with self._lock:
            return self._counters.get(_key(name, labels), 0)

    def counter_total(self, name: str) -> int:
        with self._lock:
            return sum(v for k, v in self._counters.items() if k == name or k.startswith(name + "{"))

    def gauge(self, name: str, **labels: Any) -> float | None:
        with self._lock:
            return self._gauges.get(_key(name, labels))

    def duration(self, name: str, **labels: Any) -> dict[str, float]:
        with self._lock:
            stat = self._durations.get(_key(name, labels))
            return stat.snapshot() if stat else DurationStat().snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_s": round(time.monotonic() - self.started_monotonic, 1),
                "counters": dict(sorted(self._counters.items())),
                "gauges": dict(sorted(self._gauges.items())),
                "durations_ms": {k: v.snapshot() for k, v in sorted(self._durations.items())},
            }
