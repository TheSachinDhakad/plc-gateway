"""Rockwell Logix 5000 driver (ControlLogix L7x/L8x, CompactLogix) over EtherNet/IP CIP.

Library: pycomm3 1.2.x ``LogixDriver`` (synchronous; runs on a dedicated per-device thread).

* Symbolic tag access: controller tags (``MyTag``), program tags
  (``Program:MainProgram.MyTag``), UDT members (``Motor.Status``), array elements
  (``Arr[3]``). Arrays of N elements are requested with pycomm3's ``Tag{N}`` syntax.
* Batch reads use pycomm3's multi-service packet packing (one call per scan class).
* CIP Security (TLS) is not supported by pycomm3: the controller path must permit
  unsecured CIP.
* Tags are validated against the controller's tag list on connect (``init_tags``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from pycomm3 import LogixDriver
from pycomm3.exceptions import CommError, PycommError

from drivers.base import BaseDriver
from drivers.codecs.datatypes import DataType, python_value_matches
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, LogixConnection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagConfig
from utils.exceptions import DriverConnectionError, TagConfigError
from utils.timeutil import utc_now

log = logging.getLogger(__name__)

LOGIX_TYPES = frozenset({
    DataType.BOOL, DataType.SINT, DataType.INT, DataType.DINT, DataType.LINT,
    DataType.USINT, DataType.UINT, DataType.UDINT, DataType.ULINT,
    DataType.REAL, DataType.LREAL, DataType.STRING, DataType.DWORD,
})
_INVALID_CHARS = set(" ;'\"\\{}")


class RockwellLogixDriver(BaseDriver):
    driver_name = "rockwell_logix"
    supports_write = True
    supports_batch_write = True
    blocking = True

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, LogixConnection)
        self.conn: LogixConnection = device.connection
        self._plc: LogixDriver | None = None
        self._unknown_tags: set[str] = set()

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        if tag.data_type not in LOGIX_TYPES:
            raise TagConfigError(f"data type {tag.data_type} not supported by Logix driver")
        if any(c in _INVALID_CHARS for c in tag.address):
            raise TagConfigError(f"invalid characters in Logix tag name {tag.address!r}")
        if tag.data_type is DataType.STRING and tag.array_length:
            raise TagConfigError("arrays of STRING are not supported")

    @staticmethod
    def _request_name(tag: TagConfig) -> str:
        return f"{tag.address}{{{tag.array_length}}}" if tag.array_length else tag.address

    @staticmethod
    def _base_name(address: str) -> str:
        name = address.split("[", 1)[0]
        if name.lower().startswith("program:"):
            prog, _, rest = name.partition(".")
            return f"{prog}.{rest.split('.', 1)[0]}"
        return name.split(".", 1)[0]

    # ------------------------------------------------------------------ connection
    def _connect_sync(self) -> None:
        plc = LogixDriver(self.conn.path, init_tags=self.conn.init_tags,
                          init_program_tags=self.conn.program_tags)
        plc.socket_timeout = self.conn.timeout_s
        if not plc.open():
            raise DriverConnectionError(f"{self.device_id}: Logix open() returned False")
        self._plc = plc
        self._unknown_tags.clear()
        if self.conn.init_tags:
            known = {name.lower() for name in plc.tags}
            for tag in self._tags:
                if self._base_name(tag.address).lower() not in known:
                    self._unknown_tags.add(tag.tag_id)
            if self._unknown_tags:
                log.warning("Logix tags not found in controller: %s", sorted(self._unknown_tags),
                            extra={"device_id": self.device_id, "operation": "connect"})

    async def connect(self) -> None:
        await self.disconnect()
        try:
            await self._run_blocking(self._connect_sync, timeout=self.conn.timeout_s * 6 + 30)
        except DriverConnectionError:
            raise
        except Exception as exc:
            raise DriverConnectionError(f"{self.device_id}: Logix connect to {self.conn.path} failed: {exc}") from exc

    def _close_sync(self) -> None:
        plc, self._plc = self._plc, None
        if plc is not None:
            try:
                plc.close()
            except Exception as exc:
                log.debug("Logix close error ignored: %s", exc)

    async def disconnect(self) -> None:
        if self._plc is None:
            return
        try:
            await self._run_blocking(self._close_sync, timeout=self.timeout_s + 2)
        except Exception as exc:
            log.warning("Logix close did not complete cleanly: %s", exc)
            self._plc = None

    def is_connected(self) -> bool:
        return self._plc is not None and bool(self._plc.connected)

    def _require(self) -> LogixDriver:
        if self._plc is None or not self._plc.connected:
            raise DriverConnectionError(f"{self.device_id}: not connected")
        return self._plc

    # ------------------------------------------------------------------ reads
    def _convert(self, tag: TagConfig, value: Any) -> tuple[Any, bool]:
        if tag.array_length:
            if not isinstance(value, list):
                return value, False
            return value, all(python_value_matches(tag.data_type, v) for v in value)
        return value, python_value_matches(tag.data_type, value)

    def _read_sync(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        plc = self._require()
        results: dict[str, RawReadResult] = {}
        to_read = []
        for tag in tags:
            if tag.tag_id in self._unknown_tags:
                results[tag.tag_id] = self.bad(tag, Quality.BAD_ADDRESS, "LOGIX_TAG_NOT_FOUND",
                                               f"tag {tag.address} not in controller tag list")
            else:
                to_read.append(tag)
        if to_read:
            t0 = time.perf_counter()
            try:
                response = plc.read(*[self._request_name(t) for t in to_read])
            except CommError as exc:
                raise DriverConnectionError(f"{self.device_id}: Logix read failed: {exc}") from exc
            except PycommError as exc:
                for tag in to_read:
                    results[tag.tag_id] = self.bad(tag, Quality.BAD_DEVICE_FAILURE, "LOGIX_ERROR", str(exc))
                response = None
            duration = (time.perf_counter() - t0) * 1000
            if response is not None:
                items = response if isinstance(response, list) else [response]
                if len(items) != len(to_read):
                    raise DriverConnectionError(f"{self.device_id}: Logix response count mismatch")
                ts = utc_now()
                for tag, item in zip(to_read, items, strict=True):
                    if item.error:
                        if not plc.connected:
                            raise DriverConnectionError(f"{self.device_id}: connection lost: {item.error}")
                        results[tag.tag_id] = self.bad(tag, Quality.BAD_ADDRESS, "LOGIX_READ_ERROR",
                                                       str(item.error), duration)
                        continue
                    value, ok = self._convert(tag, item.value)
                    if not ok:
                        results[tag.tag_id] = self.bad(
                            tag, Quality.BAD_TYPE_MISMATCH, "LOGIX_TYPE_MISMATCH",
                            f"controller type {item.type} does not match configured {tag.data_type}", duration)
                    else:
                        results[tag.tag_id] = RawReadResult(tag.tag_id, value, Quality.GOOD, ts,
                                                            "gateway_read", duration_ms=duration)
        return [results[t.tag_id] for t in tags]

    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        if not tags:
            return []
        return await self._run_blocking(self._read_sync, list(tags))

    # ------------------------------------------------------------------ writes
    def _write_sync(self, items: Sequence[tuple[TagConfig, TagValue]]) -> list[WriteResult]:
        plc = self._require()
        t0 = time.perf_counter()
        try:
            response = plc.write(*[(self._request_name(t), v) for t, v in items])
        except CommError as exc:
            duration = (time.perf_counter() - t0) * 1000
            return [self.write_failed(t, "LOGIX_COMM", str(exc), duration, uncertain=True) for t, _ in items]
        except PycommError as exc:
            duration = (time.perf_counter() - t0) * 1000
            return [self.write_failed(t, "LOGIX_ERROR", str(exc), duration) for t, _ in items]
        duration = (time.perf_counter() - t0) * 1000
        responses = response if isinstance(response, list) else [response]
        out = []
        for (tag, _), item in zip(items, responses, strict=True):
            if item.error:
                out.append(self.write_failed(tag, "LOGIX_WRITE_ERROR", str(item.error), duration,
                                             uncertain=not plc.connected))
            else:
                out.append(self.write_ok(tag, duration))
        return out

    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        return (await self.write_tags([(tag, value)]))[0]

    async def write_tags(self, items: Sequence[tuple[TagConfig, TagValue]]) -> list[WriteResult]:
        return await self._run_blocking(self._write_sync, list(items))

    # ------------------------------------------------------------------ info / health
    def _info_sync(self) -> DeviceInfo:
        plc = self._require()
        raw = dict(plc.info or {})
        info = DeviceInfo(self.device_id, self.driver_name, vendor=str(raw.get("vendor") or "Rockwell Automation"))
        info.model = str(raw.get("product_name") or "") or None
        rev = raw.get("revision")
        if isinstance(rev, dict):
            info.firmware = f"{rev.get('major')}.{rev.get('minor')}"
        elif rev is not None:
            info.firmware = str(rev)
        info.serial_number = str(raw.get("serial") or "") or None
        info.extra = {k: str(v) for k, v in raw.items() if k in ("name", "product_type", "product_code",
                                                                    "keyswitch", "status")}
        return info

    async def get_device_info(self) -> DeviceInfo:
        return await self._run_blocking(self._info_sync)

    async def health_check(self) -> DriverHealth:
        t0 = time.perf_counter()
        if not self.is_connected():
            return DriverHealth(self.device_id, False, False, detail="not connected", checked_at=utc_now())
        try:
            result = await self._run_blocking(lambda: self._require().get_plc_time())
            ok = not getattr(result, "error", None)
            return DriverHealth(self.device_id, True, ok, (time.perf_counter() - t0) * 1000,
                                None if ok else str(result.error), utc_now())
        except Exception as exc:
            return DriverHealth(self.device_id, self.is_connected(), False, detail=str(exc), checked_at=utc_now())
