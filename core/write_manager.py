"""Controlled, audited PLC writes (docs/ARCHITECTURE.md §9, docs/SECURITY.md §6).

Gate order — the first failing gate rejects the request:

1. principal authorized (role listed on the tag's write.allowed_roles)
2. idempotency: a known request_id returns the stored outcome without re-executing
3. global read_only_mode off, kill switch file absent, kill switch env != 1
4. device.write_enabled, tag.access == read_write
5. strict type validation + engineering limits
6. command persisted as PENDING (audit store must be available: fail closed)
7. device connected
8. write with timeout; timeout/connection loss after send -> UNCERTAIN (never auto-retried)
9. optional read-back verification -> SUCCESS / MISMATCH
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from core.tag_manager import TagManager
from models.config import GatewayConfig
from models.reading import Quality, TagValue, WriteResult, WriteStatus
from models.tag import TagAccess, TagConfig
from utils.exceptions import DatabaseError, DriverConnectionError, WriteRejectedError
from utils.timeutil import utc_now
from utils.validators import REQUEST_ID_RE, check_engineering_limits, coerce_write_value

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Principal:
    name: str
    roles: frozenset[str]


@dataclass(slots=True)
class WriteCommand:
    request_id: str
    plant_id: str
    device_id: str
    tag_id: str
    principal: str
    requested_value: Any
    reason: str | None
    status: WriteStatus = WriteStatus.PENDING
    readback_value: Any = None
    error_code: str | None = None
    error_message: str | None = None
    requested_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "plant_id": self.plant_id, "device_id": self.device_id,
            "tag_id": self.tag_id, "principal": self.principal, "requested_value": self.requested_value,
            "reason": self.reason, "status": self.status.value, "readback_value": self.readback_value,
            "error_code": self.error_code, "error_message": self.error_message,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
        }


class WriteAuditStore(Protocol):
    async def get_write_command(self, request_id: str) -> WriteCommand | None: ...
    async def insert_write_command(self, cmd: WriteCommand) -> None: ...
    async def update_write_command(self, cmd: WriteCommand) -> None: ...
    async def insert_audit(self, *, action: str, principal: str | None, result: str,
                           device_id: str | None = None, tag_id: str | None = None,
                           request_id: str | None = None, detail: dict[str, Any] | None = None) -> None: ...


class WriteTarget(Protocol):
    device_id: str

    async def write(self, tag: TagConfig, value: TagValue, timeout_s: float) -> WriteResult: ...
    async def read_now(self, tag: TagConfig, timeout_s: float) -> Any: ...


class WriteManager:
    def __init__(self, config: GatewayConfig, tag_manager: TagManager, store: WriteAuditStore,
                 targets: dict[str, WriteTarget]) -> None:
        self.config = config
        self.tags = tag_manager
        self.store = store
        self.targets = targets
        self._recent: dict[str, WriteCommand] = {}
        self._inflight: set[str] = set()
        self._rate: dict[str, deque[float]] = defaultdict(deque)
        s = config.settings
        self._kill_file: Path = config.resolve_path(s.gateway.write_kill_switch_file)

    # ------------------------------------------------------------------ gates
    def writes_globally_enabled(self) -> tuple[bool, str | None]:
        s = self.config.settings
        if s.gateway.read_only_mode:
            return False, "GATEWAY_READ_ONLY"
        if os.environ.get(s.writes.kill_switch_env, "0").strip() == "1":
            return False, "KILL_SWITCH_ENV"
        if self._kill_file.exists():
            return False, "KILL_SWITCH_FILE"
        return True, None

    def _check_rate(self, principal: str) -> None:
        limit = self.config.settings.writes.rate_limit_per_min
        now = time.monotonic()
        window = self._rate[principal]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            raise WriteRejectedError("write rate limit exceeded", code="RATE_LIMITED", http_status=429)
        window.append(now)

    async def _audit(self, cmd: WriteCommand, action: str, result: str, detail: dict[str, Any] | None = None) -> None:
        try:
            await self.store.insert_audit(action=action, principal=cmd.principal, result=result,
                                          device_id=cmd.device_id, tag_id=cmd.tag_id, request_id=cmd.request_id,
                                          detail=detail)
        except Exception as exc:  # audit failure is logged loudly but must not mask the outcome
            log.error("audit insert failed: %s", exc, extra={"request_id": cmd.request_id})

    async def _reject(self, cmd: WriteCommand, exc: WriteRejectedError, persist: bool) -> None:
        cmd.status = WriteStatus.REJECTED
        cmd.error_code = exc.code
        cmd.error_message = str(exc)
        cmd.completed_at = utc_now()
        log.warning("write rejected: %s", exc, extra={"device_id": cmd.device_id, "tag_id": cmd.tag_id,
                                                      "request_id": cmd.request_id, "principal": cmd.principal,
                                                      "error_code": exc.code, "operation": "write"})
        if persist:
            try:
                await self.store.insert_write_command(cmd)
            except Exception as db_exc:
                log.error("could not persist rejected write: %s", db_exc)
        await self._audit(cmd, "PLC_WRITE", "REJECTED", {"code": exc.code, "message": str(exc)})

    # ------------------------------------------------------------------ main entry
    async def submit(self, principal: Principal, device_id: str, tag_id: str, value: Any, request_id: str,
                     reason: str | None = None) -> WriteCommand:
        if not REQUEST_ID_RE.match(request_id):
            raise WriteRejectedError("request_id must be 8-64 chars [A-Za-z0-9_-:.]", code="INVALID_REQUEST_ID",
                                     http_status=422)
        device = self.tags.device(device_id)
        tag = self.tags.tag(device_id, tag_id)
        if device is None or tag is None:
            raise WriteRejectedError("unknown device or tag", code="NOT_FOUND", http_status=404)
        cmd = WriteCommand(request_id, device.plant_id, device_id, tag_id, principal.name, value, reason)

        # 1. authorization (before revealing anything else about the tag)
        allowed = set(tag.write.allowed_roles) if tag.write else set()
        if not (allowed & set(principal.roles)):
            exc = WriteRejectedError("principal not authorized to write this tag", code="FORBIDDEN", http_status=403)
            await self._reject(cmd, exc, persist=False)
            raise exc

        # 2. idempotency
        if request_id in self._inflight:
            raise WriteRejectedError("request with this request_id is in progress", code="IN_PROGRESS",
                                     http_status=409)
        previous = self._recent.get(request_id)
        if previous is None:
            try:
                previous = await self.store.get_write_command(request_id)
            except Exception as exc:
                raise WriteRejectedError(f"audit store unavailable, writes disabled: {exc}",
                                         code="AUDIT_UNAVAILABLE", http_status=503) from exc
        if previous is not None:
            if (previous.device_id, previous.tag_id) != (device_id, tag_id) or previous.requested_value != value:
                raise WriteRejectedError("request_id already used for a different command",
                                         code="REQUEST_ID_CONFLICT", http_status=409)
            return previous

        self._inflight.add(request_id)
        try:
            return await self._execute(principal, device, tag, cmd, value)
        finally:
            self._inflight.discard(request_id)

    async def _execute(self, principal: Principal, device: Any, tag: TagConfig, cmd: WriteCommand,
                       value: Any) -> WriteCommand:
        try:
            self._check_rate(principal.name)
            enabled, code = self.writes_globally_enabled()
            if not enabled:
                raise WriteRejectedError(f"writes disabled ({code})", code=code or "WRITES_DISABLED", http_status=403)
            if not device.write_enabled:
                raise WriteRejectedError("writes not enabled for this device", code="DEVICE_READ_ONLY")
            if tag.access is not TagAccess.READ_WRITE or tag.write is None:
                raise WriteRejectedError("tag is read-only", code="TAG_READ_ONLY")
            if not tag.enabled:
                raise WriteRejectedError("tag is disabled", code="TAG_DISABLED")
            coerced = coerce_write_value(tag.data_type, value, array_length=tag.array_length,
                                         string_length=tag.string_length)
            check_engineering_limits(coerced, tag.write.min, tag.write.max)
            raw_value = coerced
            if tag.scaling is not None and not isinstance(coerced, list):
                raw_value = tag.scaling.invert(float(coerced))
                if tag.data_type.value not in ("REAL", "LREAL"):
                    raw_value = round(raw_value)
                raw_value = coerce_write_value(tag.data_type, raw_value)
        except WriteRejectedError as exc:
            await self._reject(cmd, exc, persist=True)
            raise

        target = self.targets.get(device.device_id)
        timeout = tag.write.timeout_s or self.config.settings.writes.default_timeout_s

        # 6. persist PENDING before touching the PLC (fail closed)
        try:
            await self.store.insert_write_command(cmd)
        except Exception as exc:
            log.error("cannot persist write command, rejecting: %s", exc)
            raise WriteRejectedError("audit store unavailable, writes disabled", code="AUDIT_UNAVAILABLE",
                                     http_status=503) from exc

        if target is None or not target_connected(target):
            cmd.status = WriteStatus.FAILED
            cmd.error_code = "DEVICE_NOT_CONNECTED"
            cmd.error_message = "device is not connected"
            await self._finish(cmd)
            return cmd

        t0 = time.perf_counter()
        try:
            result = await target.write(tag, raw_value, timeout)
            cmd.status = result.status
            cmd.error_code = result.error_code
            cmd.error_message = result.error_message
        except TimeoutError:
            cmd.status = WriteStatus.UNCERTAIN
            cmd.error_code = "WRITE_TIMEOUT"
            cmd.error_message = f"no response within {timeout}s; verify on PLC before re-issuing"
        except DriverConnectionError as exc:
            cmd.status = WriteStatus.FAILED
            cmd.error_code = "DEVICE_NOT_CONNECTED"
            cmd.error_message = str(exc)
        except Exception as exc:
            cmd.status = WriteStatus.UNCERTAIN
            cmd.error_code = "WRITE_EXCEPTION"
            cmd.error_message = f"{type(exc).__name__}: {exc}"
        cmd.duration_ms = (time.perf_counter() - t0) * 1000

        if cmd.status is WriteStatus.SUCCESS and tag.write.verify_readback:
            try:
                rb = await target.read_now(tag, timeout)
                if rb.quality is Quality.GOOD:
                    cmd.readback_value = rb.value
                    if not _values_match(raw_value, rb.value, tag.write.readback_tolerance):
                        cmd.status = WriteStatus.MISMATCH
                        cmd.error_code = "READBACK_MISMATCH"
                        cmd.error_message = f"read back {rb.value!r} (raw), expected {raw_value!r}"
                else:
                    cmd.status = WriteStatus.UNCERTAIN
                    cmd.error_code = "READBACK_FAILED"
                    cmd.error_message = f"read-back quality {rb.quality}"
            except Exception as exc:
                cmd.status = WriteStatus.UNCERTAIN
                cmd.error_code = "READBACK_FAILED"
                cmd.error_message = f"{type(exc).__name__}: {exc}"
        await self._finish(cmd)
        return cmd

    async def _finish(self, cmd: WriteCommand) -> None:
        cmd.completed_at = utc_now()
        self._recent[cmd.request_id] = cmd
        if len(self._recent) > 10_000:
            self._recent.pop(next(iter(self._recent)))
        try:
            await self.store.update_write_command(cmd)
        except (DatabaseError, Exception) as exc:
            log.error("could not persist write outcome %s: %s", cmd.status, exc, extra={"request_id": cmd.request_id})
        level = logging.INFO if cmd.status is WriteStatus.SUCCESS else logging.WARNING
        log.log(level, "write %s", cmd.status.value, extra={
            "device_id": cmd.device_id, "tag_id": cmd.tag_id, "request_id": cmd.request_id,
            "principal": cmd.principal, "error_code": cmd.error_code, "duration_ms": cmd.duration_ms,
            "operation": "write"})
        await self._audit(cmd, "PLC_WRITE", cmd.status.value,
                          {"requested_value": cmd.requested_value, "readback_value": cmd.readback_value,
                           "error_code": cmd.error_code})


def target_connected(target: Any) -> bool:
    driver = getattr(target, "driver", None)
    return bool(driver.is_connected()) if driver is not None else True


def _values_match(expected: Any, actual: Any, tolerance: float) -> bool:
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _values_match(e, a, tolerance) for e, a in zip(expected, actual, strict=True))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        if isinstance(expected, float) or isinstance(actual, float):
            # REAL is float32 on the wire: compare with relative tolerance as well
            return abs(expected - actual) <= max(tolerance, abs(expected) * 1e-6)
        return expected == actual
    return expected == actual


async def wait_closed(_: asyncio.Event) -> None:  # pragma: no cover - helper for symmetry
    return None
