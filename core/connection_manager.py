"""Per-device connection lifecycle: reconnect with jittered backoff and a circuit breaker.

Emits exactly one "lost" notification per outage and one "restored" notification when the
connection comes back (no event storms while a device stays offline).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from drivers.base import BaseDriver
from models.plc import DeviceConfig, DeviceStatus
from services.metrics import Metrics
from utils.backoff import Backoff, CircuitBreaker, CircuitState
from utils.exceptions import GatewayError
from utils.timeutil import utc_now

log = logging.getLogger(__name__)

Callback = Callable[[str], Awaitable[None]]


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; return True if ``stop`` was set."""
    if seconds <= 0:
        return stop.is_set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
    return stop.is_set()


class ConnectionManager:
    def __init__(
        self,
        device: DeviceConfig,
        driver: BaseDriver,
        metrics: Metrics,
        *,
        on_lost: Callback | None = None,
        on_restored: Callback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.driver = driver
        self.metrics = metrics
        self._on_lost = on_lost
        self._on_restored = on_restored
        r = device.reconnect
        self.backoff = Backoff(r.initial_delay_s, r.max_delay_s, r.jitter)
        cb = device.circuit_breaker
        self.breaker = CircuitBreaker(cb.failure_threshold, cb.open_duration_s, clock=clock)
        self.status = DeviceStatus.DISCONNECTED
        self.last_error: str | None = None
        self.last_connected_at = None
        self.last_disconnected_at = None
        self.connect_count = 0
        self._comm_ok: bool | None = None   # None = never connected yet

    def _set_status(self, status: DeviceStatus) -> None:
        self.status = status
        self.metrics.set_gauge("plc_connected", 1 if status is DeviceStatus.CONNECTED else 0,
                               device=self.device.device_id)

    async def _notify_lost(self, reason: str) -> None:
        if self._comm_ok is False:
            return
        self._comm_ok = False
        self.last_disconnected_at = utc_now()
        if self._on_lost:
            await self._on_lost(reason)

    async def ensure_connected(self, stop: asyncio.Event) -> bool:
        """Return True when connected; otherwise wait (backoff / open circuit) and return False."""
        if self.driver.is_connected():
            if self.status is not DeviceStatus.CONNECTED:
                self._set_status(DeviceStatus.CONNECTED)
            return True
        if not self.breaker.allow_request():
            self._set_status(DeviceStatus.CIRCUIT_OPEN)
            await sleep_or_stop(stop, min(self.breaker.seconds_until_half_open(), 5.0))
            return False
        self._set_status(DeviceStatus.CONNECTING)
        try:
            await self.driver.connect()
        except GatewayError as exc:
            await self._connect_failed(str(exc), stop)
            return False
        except Exception as exc:  # unexpected library error: still isolate the device
            log.exception("unexpected error while connecting", extra={"device_id": self.device.device_id})
            await self._connect_failed(f"{type(exc).__name__}: {exc}", stop)
            return False
        self.breaker.record_success()
        self.backoff.reset()
        self.connect_count += 1
        if self.connect_count > 1:
            self.metrics.inc("reconnects_total", device=self.device.device_id)
        self.last_connected_at = utc_now()
        self.last_error = None
        self._set_status(DeviceStatus.CONNECTED)
        was_lost = self._comm_ok is False
        self._comm_ok = True
        if was_lost and self._on_restored:
            await self._on_restored("connection restored")
        return True

    async def _connect_failed(self, reason: str, stop: asyncio.Event) -> None:
        self.last_error = reason
        self.breaker.record_failure()
        self.metrics.inc("connect_failures_total", device=self.device.device_id)
        state = self.breaker.state
        self._set_status(DeviceStatus.CIRCUIT_OPEN if state is CircuitState.OPEN else DeviceStatus.DISCONNECTED)
        delay = self.backoff.next_delay()
        log.warning("connect failed (attempt %d, retry in %.1fs): %s", self.backoff.attempt, delay, reason,
                    extra={"device_id": self.device.device_id, "operation": "connect", "error_code": "CONNECT"})
        await self._notify_lost(reason)
        await sleep_or_stop(stop, delay)

    async def handle_failure(self, reason: str) -> None:
        """Called by the worker when an established connection fails during I/O."""
        self.last_error = reason
        log.warning("connection lost: %s", reason,
                    extra={"device_id": self.device.device_id, "operation": "read", "error_code": "CONNECTION"})
        await self.driver.disconnect()
        self.breaker.record_failure()
        self._set_status(DeviceStatus.DISCONNECTED)
        await self._notify_lost(reason)

    async def close(self) -> None:
        await self.driver.disconnect()
        self._set_status(DeviceStatus.STOPPED)
