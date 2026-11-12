"""Siemens S7-300/400/1200/1500 driver over classic S7 communication (ISO-on-TCP, port 102).

Library: python-snap7 3.5.x (pure-Python implementation; synchronous client used on a
dedicated per-device thread).

Requirements on S7-1200/1500 (see docs/PLC_CONFIGURATION.md):
* "Permit access with PUT/GET communication" enabled in CPU protection settings.
* Data blocks accessed by absolute address must have "Optimized block access" DISABLED.
* S7CommPlus (symbolic access to optimized DBs) is NOT supported — use the CPU's OPC UA server.

Batching: tags are grouped by (area, DB) and coalesced into contiguous byte blocks
(``max_gap_bytes``/``max_block_bytes``); each block is one ``read_area`` request. If a block
fails with an address error, the driver falls back to per-tag reads to isolate bad tags.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import snap7
from snap7.error import S7ConnectionError, S7Error, S7TimeoutError
from snap7.type import Area, WordLen

from drivers.base import BaseDriver, coalesce_ranges
from drivers.codecs.siemens_address import S7Address, decode_s7, encode_s7, parse_s7_address
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, S7Connection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagConfig
from utils.exceptions import (
    AddressError,
    DriverConnectionError,
    EncodingError,
    TagConfigError,
)
from utils.timeutil import utc_now

log = logging.getLogger(__name__)

_AREAS = {"DB": Area.DB, "MK": Area.MK, "PE": Area.PE, "PA": Area.PA}
_CONNECTION_TYPES = {"PG": 1, "OP": 2, "S7_BASIC": 3}
_CONNECTION_ERRORS = (S7ConnectionError, S7TimeoutError, ConnectionError, OSError, TimeoutError)


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, _CONNECTION_ERRORS):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("not connected", "connection", "socket", "iso", "timeout"))


class SiemensS7Driver(BaseDriver):
    driver_name = "siemens_s7"
    supports_write = True
    blocking = True

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, S7Connection)
        self.conn: S7Connection = device.connection
        self._client: snap7.Client | None = None
        self._addresses: dict[str, S7Address] = {}

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        try:
            parse_s7_address(tag.address, tag.data_type, array_length=tag.array_length,
                             string_length=tag.string_length)
        except AddressError as exc:
            raise TagConfigError(str(exc)) from exc

    def _addr(self, tag: TagConfig) -> S7Address:
        addr = self._addresses.get(tag.tag_id)
        if addr is None:
            addr = parse_s7_address(tag.address, tag.data_type, array_length=tag.array_length,
                                    string_length=tag.string_length)
            self._addresses[tag.tag_id] = addr
        return addr

    # ------------------------------------------------------------------ connection
    def _connect_sync(self) -> None:
        client = snap7.Client()
        client.set_connection_type(_CONNECTION_TYPES[self.conn.connection_type])
        client.connect(self.conn.host, self.conn.rack, self.conn.slot, self.conn.port)
        if not client.get_connected():
            raise DriverConnectionError(f"{self.device_id}: connect returned but client not connected")
        # python-snap7 3.5 connects with a fixed 5 s socket timeout and re-applies
        # ``connection.timeout`` on each receive; apply the configured value after connect.
        connection = getattr(client, "connection", None)
        if connection is not None and hasattr(connection, "timeout"):
            connection.timeout = float(self.conn.timeout_s)
        self._client = client

    async def connect(self) -> None:
        await self.disconnect()
        try:
            await self._run_blocking(self._connect_sync)
        except DriverConnectionError:
            raise
        except Exception as exc:
            raise DriverConnectionError(
                f"{self.device_id}: S7 connect to {self.conn.host} rack={self.conn.rack} "
                f"slot={self.conn.slot} failed: {exc}") from exc
        log.info("S7 connected", extra={"device_id": self.device_id, "operation": "connect"})

    def _disconnect_sync(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception as exc:  # disconnect must never raise
            log.debug("S7 disconnect error ignored: %s", exc)
        try:
            client.destroy()
        except Exception as exc:
            log.debug("S7 destroy error ignored: %s", exc)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._run_blocking(self._disconnect_sync, timeout=self.timeout_s + 2)
        except Exception as exc:
            log.warning("S7 disconnect did not complete cleanly: %s", exc)
            self._client = None

    def is_connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.get_connected())
        except Exception:
            return False

    def _require_client(self) -> snap7.Client:
        if self._client is None:
            raise DriverConnectionError(f"{self.device_id}: not connected")
        return self._client

    # ------------------------------------------------------------------ reads
    def _read_block(self, area: str, db: int, start: int, size: int) -> bytearray:
        client = self._require_client()
        return client.read_area(_AREAS[area], db, start, size)

    def _read_sync(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        results: dict[str, RawReadResult] = {}
        groups: dict[tuple[str, int], list[tuple[int, int, TagConfig]]] = defaultdict(list)
        for tag in tags:
            try:
                addr = self._addr(tag)
            except AddressError as exc:
                results[tag.tag_id] = self.bad(tag, Quality.BAD_CONFIG_ERROR, "S7_ADDRESS", str(exc))
                continue
            groups[(addr.area, addr.db_number)].append((addr.start, addr.size, tag))

        for (area, db), items in groups.items():
            for b_start, b_len, block_tags in coalesce_ranges(
                    items, max_gap=self.conn.max_gap_bytes, max_span=self.conn.max_block_bytes):
                t0 = time.perf_counter()
                try:
                    data = self._read_block(area, db, b_start, b_len)
                except Exception as exc:
                    if _is_connection_error(exc):
                        raise DriverConnectionError(f"{self.device_id}: read failed: {exc}") from exc
                    # address-level failure: isolate per tag
                    for tag in block_tags:
                        results[tag.tag_id] = self._read_single(tag)
                    continue
                duration = (time.perf_counter() - t0) * 1000
                ts = utc_now()
                for tag in block_tags:
                    addr = self._addr(tag)
                    try:
                        value = decode_s7(data, addr, base=b_start)
                        results[tag.tag_id] = RawReadResult(tag.tag_id, value, Quality.GOOD, ts,
                                                            "gateway_read", duration_ms=duration)
                    except (EncodingError, ValueError) as exc:
                        results[tag.tag_id] = self.bad(tag, Quality.BAD_TYPE_MISMATCH, "S7_DECODE", str(exc),
                                                       duration)
        return [results[t.tag_id] for t in tags]

    def _read_single(self, tag: TagConfig) -> RawReadResult:
        addr = self._addr(tag)
        t0 = time.perf_counter()
        try:
            data = self._read_block(addr.area, addr.db_number, addr.start, addr.size)
            value = decode_s7(data, addr, base=addr.start)
            return self.good(tag, value, (time.perf_counter() - t0) * 1000)
        except EncodingError as exc:
            return self.bad(tag, Quality.BAD_TYPE_MISMATCH, "S7_DECODE", str(exc))
        except S7Error as exc:
            if _is_connection_error(exc):
                raise DriverConnectionError(f"{self.device_id}: read failed: {exc}") from exc
            return self.bad(tag, Quality.BAD_ADDRESS, "S7_ADDRESS_REJECTED",
                            f"{exc} (check DB exists, is non-optimized, PUT/GET enabled)")
        except Exception as exc:
            if _is_connection_error(exc):
                raise DriverConnectionError(f"{self.device_id}: read failed: {exc}") from exc
            return self.bad(tag, Quality.BAD_DEVICE_FAILURE, "S7_ERROR", str(exc))

    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        if not tags:
            return []
        self._require_client()
        return await self._run_blocking(self._read_sync, list(tags))

    # ------------------------------------------------------------------ writes
    def _write_sync(self, tag: TagConfig, value: TagValue) -> WriteResult:
        client = self._require_client()
        addr = self._addr(tag)
        t0 = time.perf_counter()
        try:
            if addr.bit is not None:
                client.write_area(_AREAS[addr.area], addr.db_number, addr.start * 8 + addr.bit,
                                  bytearray([1 if value else 0]), WordLen.Bit)
            else:
                client.write_area(_AREAS[addr.area], addr.db_number, addr.start,
                                  bytearray(encode_s7(value, addr)))
        except EncodingError as exc:
            return self.write_failed(tag, "S7_ENCODE", str(exc))
        except Exception as exc:
            if _is_connection_error(exc):
                # request may or may not have reached the PLC
                return self.write_failed(tag, "S7_CONNECTION", str(exc),
                                         (time.perf_counter() - t0) * 1000, uncertain=True)
            return self.write_failed(tag, "S7_WRITE_REJECTED", str(exc), (time.perf_counter() - t0) * 1000)
        return self.write_ok(tag, (time.perf_counter() - t0) * 1000)

    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        self._require_client()
        return await self._run_blocking(self._write_sync, tag, value)

    # ------------------------------------------------------------------ info / health
    def _info_sync(self) -> DeviceInfo:
        client = self._require_client()
        info = DeviceInfo(self.device_id, self.driver_name, vendor="Siemens")
        try:
            cpu = client.get_cpu_info()
            info.model = _text(cpu.ModuleTypeName)
            info.serial_number = _text(cpu.SerialNumber)
            info.extra.update({"as_name": _text(cpu.ASName), "module_name": _text(cpu.ModuleName)})
        except Exception as exc:
            info.extra["cpu_info_error"] = str(exc)
        try:
            order = client.get_order_code()
            info.extra["order_code"] = _text(order.OrderCode)
            info.firmware = f"V{order.V1}.{order.V2}.{order.V3}"
        except Exception as exc:
            info.extra["order_code_error"] = str(exc)
        try:
            info.extra["cpu_state"] = client.get_cpu_state()
        except Exception as exc:
            info.extra["cpu_state_error"] = str(exc)
        return info

    async def get_device_info(self) -> DeviceInfo:
        return await self._run_blocking(self._info_sync)

    async def health_check(self) -> DriverHealth:
        t0 = time.perf_counter()
        if not self.is_connected():
            return DriverHealth(self.device_id, False, False, detail="not connected", checked_at=utc_now())
        try:
            state = await self._run_blocking(lambda: self._require_client().get_cpu_state())
            return DriverHealth(self.device_id, True, True, (time.perf_counter() - t0) * 1000,
                                f"cpu_state={state}", utc_now())
        except Exception as exc:
            return DriverHealth(self.device_id, self.is_connected(), False, detail=str(exc), checked_at=utc_now())


def _text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
    return str(value).strip()
