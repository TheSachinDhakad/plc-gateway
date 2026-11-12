"""Common PLC driver contract (see docs/ARCHITECTURE.md §4).

Rules every driver follows:

* ``read_tags`` never raises for per-tag problems: it returns a ``RawReadResult`` with a
  BAD quality. It raises :class:`DriverConnectionError` only when the connection itself is
  unusable, which triggers the reconnect path in the ConnectionManager.
* Unsupported operations raise :class:`UnsupportedOperationError` — never silent no-ops.
* Drivers never invent values.
* Retry/backoff is owned by the ConnectionManager, not by drivers.
"""

from __future__ import annotations

import abc
import asyncio
import functools
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from typing import Any, ClassVar, TypeVar

from models.plc import DeviceConfig, DeviceInfo, DriverHealth
from models.reading import Quality, RawReadResult, TagValue, WriteResult, WriteStatus
from models.tag import TagConfig
from utils.exceptions import (
    DriverTimeoutError,
    PlatformNotSupportedError,
    UnsupportedOperationError,
)
from utils.timeutil import utc_now

T = TypeVar("T")


class BaseDriver(abc.ABC):
    driver_name: ClassVar[str]
    supports_write: ClassVar[bool] = True
    supports_batch_write: ClassVar[bool] = False
    platforms: ClassVar[frozenset[str]] = frozenset({"win32", "darwin", "linux"})
    blocking: ClassVar[bool] = False   # True -> requires a dedicated single-thread executor

    def __init__(self, device: DeviceConfig, *, executor: Executor | None = None) -> None:
        self.check_platform()
        if self.blocking and executor is None:
            raise ValueError(f"{self.driver_name} requires a dedicated executor")
        self.device = device
        self.device_id = device.device_id
        self._executor = executor
        self._tags: list[TagConfig] = []

    # ------------------------------------------------------------------ class-level checks
    @classmethod
    def check_platform(cls) -> None:
        if sys.platform not in cls.platforms:
            raise PlatformNotSupportedError(
                f"driver {cls.driver_name} is not available on platform {sys.platform}")

    @classmethod
    @abc.abstractmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        """Raise TagConfigError/AddressError if the tag cannot be served by this driver."""

    # ------------------------------------------------------------------ lifecycle
    async def register_tags(self, tags: Sequence[TagConfig]) -> None:
        """Called before connect() with all enabled tags of the device."""
        self._tags = list(tags)

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Release the connection. Must never raise."""

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    # ------------------------------------------------------------------ I/O
    async def read_tag(self, tag: TagConfig) -> RawReadResult:
        return (await self.read_tags([tag]))[0]

    @abc.abstractmethod
    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]: ...

    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        raise UnsupportedOperationError(f"{self.driver_name} does not support writes")

    async def write_tags(self, items: Sequence[tuple[TagConfig, TagValue]]) -> list[WriteResult]:
        if not self.supports_write:
            raise UnsupportedOperationError(f"{self.driver_name} does not support writes")
        return [await self.write_tag(tag, value) for tag, value in items]

    @abc.abstractmethod
    async def get_device_info(self) -> DeviceInfo: ...

    async def health_check(self) -> DriverHealth:
        start = time.perf_counter()
        connected = self.is_connected()
        return DriverHealth(self.device_id, connected=connected, ok=connected,
                            latency_ms=(time.perf_counter() - start) * 1000, checked_at=utc_now())

    # ------------------------------------------------------------------ helpers
    @property
    def timeout_s(self) -> float:
        return float(getattr(self.device.connection, "timeout_s", 5.0))

    async def _run_blocking(self, fn: Callable[..., T], *args: Any, timeout: float | None = None) -> T:
        """Run a blocking library call on this device's dedicated thread.

        The outer timeout is a safety net above the library's own socket timeout; a call that
        exceeds it is reported as a timeout (the connection is then treated as broken).
        """
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._executor, functools.partial(fn, *args))
        limit = timeout if timeout is not None else self.timeout_s * 2 + 1
        try:
            return await asyncio.wait_for(fut, limit)
        except TimeoutError as exc:
            raise DriverTimeoutError(f"{self.device_id}: operation exceeded {limit:.1f}s") from exc

    @staticmethod
    def bad(tag: TagConfig, quality: Quality, code: str, message: str,
            duration_ms: float | None = None) -> RawReadResult:
        return RawReadResult(tag.tag_id, None, quality, utc_now(), "gateway_read", code, message, duration_ms)

    @staticmethod
    def good(tag: TagConfig, value: TagValue, duration_ms: float | None = None) -> RawReadResult:
        return RawReadResult(tag.tag_id, value, Quality.GOOD, utc_now(), "gateway_read", None, None,
                             duration_ms)

    @staticmethod
    def write_ok(tag: TagConfig, duration_ms: float) -> WriteResult:
        return WriteResult(tag.tag_id, WriteStatus.SUCCESS, duration_ms=duration_ms)

    @staticmethod
    def write_failed(tag: TagConfig, code: str, message: str, duration_ms: float | None = None,
                     *, uncertain: bool = False) -> WriteResult:
        status = WriteStatus.UNCERTAIN if uncertain else WriteStatus.FAILED
        return WriteResult(tag.tag_id, status, code, message, duration_ms)


def coalesce_ranges(items: Sequence[tuple[int, int, Any]], *, max_gap: int, max_span: int) -> list[
        tuple[int, int, list[Any]]]:
    """Merge (start, length, payload) ranges into blocks.

    Returns [(block_start, block_length, [payloads])]. Ranges longer than ``max_span`` get their
    own block (the caller must chunk or reject them).
    """
    out: list[tuple[int, int, list[Any]]] = []
    for start, length, payload in sorted(items, key=lambda x: (x[0], x[1])):
        end = start + length
        if out:
            b_start, b_len, payloads = out[-1]
            b_end = b_start + b_len
            new_end = max(b_end, end)
            if start <= b_end + max_gap and new_end - b_start <= max_span:
                out[-1] = (b_start, new_end - b_start, [*payloads, payload])
                continue
        out.append((start, length, [payload]))
    return out
