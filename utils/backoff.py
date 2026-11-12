"""Retry backoff (exponential, jittered) and a circuit breaker.

Both are pure state machines with an injectable clock so they can be unit-tested
deterministically.
"""

from __future__ import annotations

import enum
import random
import time
from collections.abc import Callable


class Backoff:
    """Exponential backoff with proportional jitter.

    delay_n = min(max_delay, initial * 2**n) * (1 ± jitter)
    """

    def __init__(
        self,
        initial_delay_s: float = 1.0,
        max_delay_s: float = 60.0,
        jitter: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        if initial_delay_s <= 0 or max_delay_s < initial_delay_s:
            raise ValueError("invalid backoff bounds")
        if not 0 <= jitter <= 1:
            raise ValueError("jitter must be within [0, 1]")
        self.initial_delay_s = initial_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter
        self._attempt = 0
        self._rng = rng or random.Random()

    @property
    def attempt(self) -> int:
        return self._attempt

    def next_delay(self) -> float:
        base = min(self.max_delay_s, self.initial_delay_s * (2 ** min(self._attempt, 30)))
        self._attempt += 1
        if self.jitter == 0:
            return base
        spread = base * self.jitter
        return max(0.0, base + self._rng.uniform(-spread, spread))

    def reset(self) -> None:
        self._attempt = 0


class CircuitState(enum.StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """CLOSED --(threshold consecutive failures)--> OPEN --(open_duration)--> HALF_OPEN.

    HALF_OPEN allows one probe; success closes the circuit, failure re-opens it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        open_duration_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.open_duration_s = open_duration_s
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self.open_count = 0

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.open_duration_s:
            self._state = CircuitState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow_request(self) -> bool:
        return self.state is not CircuitState.OPEN

    def seconds_until_half_open(self) -> float:
        if self.state is not CircuitState.OPEN:
            return 0.0
        return max(0.0, self.open_duration_s - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        state = self.state
        if state is CircuitState.HALF_OPEN or (
            state is CircuitState.CLOSED and self._failures >= self.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self.open_count += 1
