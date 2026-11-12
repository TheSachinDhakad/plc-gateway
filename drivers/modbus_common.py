"""Shared Modbus logic for TCP and RTU drivers (pymodbus 3.16 async clients).

Address grammar (ZERO-BASED protocol addresses)::

    [unit@]table:offset[.bit]
    table ∈ holding | input | coil | discrete
    e.g.  holding:100   input:0   coil:5   discrete:3   holding:20.7   5@holding:0

``40101``-style notation is rejected on purpose (off-by-one risk); see PLC_CONFIGURATION.md.
Reads are coalesced per (unit, table) into blocks up to ``max_registers_per_read`` /
``max_bits_per_read`` with at most ``max_gap`` unused addresses inside a block.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from drivers.base import BaseDriver, coalesce_ranges
from drivers.codecs.datatypes import DataType
from drivers.codecs.register_codec import REGISTER_TYPES, decode_registers, encode_registers, register_count
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, ModbusRtuConnection, ModbusTcpConnection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagAccess, TagConfig
from utils.exceptions import AddressError, DriverConnectionError, EncodingError, TagConfigError
from utils.timeutil import utc_now

log = logging.getLogger(__name__)

_ADDR_RE = re.compile(r"^(?:(\d{1,3})@)?(holding|input|coil|discrete):(\d{1,5})(?:\.(\d{1,2}))?$", re.I)
BIT_TABLES = frozenset({"coil", "discrete"})
EXCEPTION_NAMES = {1: "ILLEGAL_FUNCTION", 2: "ILLEGAL_DATA_ADDRESS", 3: "ILLEGAL_DATA_VALUE",
                   4: "DEVICE_FAILURE", 5: "ACKNOWLEDGE", 6: "DEVICE_BUSY", 10: "GATEWAY_PATH_UNAVAILABLE",
                   11: "GATEWAY_TARGET_NO_RESPONSE"}


@dataclass(frozen=True, slots=True)
class ModbusAddress:
    unit_id: int
    table: str
    offset: int
    bit: int | None
    count: int              # registers or bits occupied

    @property
    def is_bit_table(self) -> bool:
        return self.table in BIT_TABLES


def parse_modbus_address(tag: TagConfig, default_unit: int) -> ModbusAddress:
    m = _ADDR_RE.match(tag.address.strip())
    if not m:
        if re.fullmatch(r"[0-4]\d{4,5}", tag.address.strip()):
            raise AddressError(f"{tag.address}: 1-based register notation is not accepted; "
                               "use e.g. holding:0 for 40001")
        raise AddressError(f"{tag.address}: expected [unit@]holding|input|coil|discrete:offset[.bit]")
    unit = int(m.group(1)) if m.group(1) else default_unit
    table, offset = m.group(2).lower(), int(m.group(3))
    bit = int(m.group(4)) if m.group(4) is not None else None
    if not 0 <= unit <= 255:
        raise AddressError("unit id must be 0..255")
    if offset > 65535:
        raise AddressError("offset must be 0..65535")
    if table in BIT_TABLES:
        if tag.data_type is not DataType.BOOL:
            raise AddressError(f"{table} requires data_type BOOL")
        if bit is not None:
            raise AddressError("bit suffix is not valid on coils/discrete inputs")
        count = tag.array_length or 1
    elif tag.data_type is DataType.BOOL:
        if bit is None or not 0 <= bit <= 15:
            raise AddressError("BOOL on a register requires a bit suffix 0..15, e.g. holding:10.3")
        if tag.array_length:
            raise AddressError("arrays of register bits are not supported")
        count = 1
    else:
        if bit is not None:
            raise AddressError("bit suffix requires data_type BOOL")
        if tag.data_type not in REGISTER_TYPES:
            raise AddressError(f"{tag.data_type} is not supported over Modbus registers")
        try:
            count = register_count(tag.data_type, tag.string_length) * (tag.array_length or 1)
        except EncodingError as exc:
            raise AddressError(str(exc)) from exc
    if offset + count > 65536:
        raise AddressError("address range exceeds 65535")
    return ModbusAddress(unit, table, offset, bit, count)


ModbusConnection = ModbusTcpConnection | ModbusRtuConnection


class ModbusDriverBase(BaseDriver):
    """Common read/write implementation; subclasses provide the pymodbus client."""

    supports_write = True

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        self.conn: ModbusConnection = device.connection  # type: ignore[assignment]
        self._client: Any = None
        self._addresses: dict[str, ModbusAddress] = {}

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        conn = device.connection
        assert isinstance(conn, ModbusTcpConnection | ModbusRtuConnection)
        try:
            addr = parse_modbus_address(tag, conn.unit_id)
        except AddressError as exc:
            raise TagConfigError(str(exc)) from exc
        limit = conn.max_bits_per_read if addr.is_bit_table else conn.max_registers_per_read
        if addr.count > limit:
            raise TagConfigError(f"tag spans {addr.count} units, exceeds per-read limit {limit}")
        if tag.access is TagAccess.READ_WRITE:
            if addr.table in ("input", "discrete"):
                raise TagConfigError(f"{addr.table} table is read-only by protocol")
            if addr.bit is not None:
                raise TagConfigError("register-bit writes are not supported (no read-modify-write)")
        if isinstance(conn, ModbusRtuConnection) and addr.unit_id == 0:
            raise TagConfigError("unit id 0 (broadcast) cannot be read")

    def _addr(self, tag: TagConfig) -> ModbusAddress:
        addr = self._addresses.get(tag.tag_id)
        if addr is None:
            addr = parse_modbus_address(tag, self.conn.unit_id)
            self._addresses[tag.tag_id] = addr
        return addr

    # ------------------------------------------------------------------ connection
    def _make_client(self) -> Any:
        raise NotImplementedError

    def _endpoint(self) -> str:
        raise NotImplementedError

    async def connect(self) -> None:
        await self.disconnect()
        client = self._make_client()
        try:
            ok = await client.connect()
        except Exception as exc:
            client.close()
            raise DriverConnectionError(f"{self.device_id}: Modbus connect {self._endpoint()} failed: {exc}") from exc
        if not ok or not client.connected:
            client.close()
            raise DriverConnectionError(f"{self.device_id}: Modbus connect {self._endpoint()} failed")
        self._client = client
        log.info("Modbus connected %s", self._endpoint(), extra={"device_id": self.device_id, "operation": "connect"})

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                log.debug("Modbus close error ignored: %s", exc)

    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.connected)

    def _require(self) -> Any:
        if self._client is None or not self._client.connected:
            raise DriverConnectionError(f"{self.device_id}: not connected")
        return self._client

    # ------------------------------------------------------------------ reads
    async def _request(self, table: str, unit: int, start: int, count: int) -> Any:
        client = self._require()
        fn = {
            "holding": client.read_holding_registers,
            "input": client.read_input_registers,
            "coil": client.read_coils,
            "discrete": client.read_discrete_inputs,
        }[table]
        try:
            return await fn(start, count=count, device_id=unit)
        except (ConnectionException, ModbusIOException, OSError, TimeoutError) as exc:
            raise DriverConnectionError(f"{self.device_id}: Modbus request failed: {exc}") from exc

    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        results: dict[str, RawReadResult] = {}
        groups: dict[tuple[int, str], list[tuple[int, int, TagConfig]]] = defaultdict(list)
        for tag in tags:
            try:
                addr = self._addr(tag)
            except AddressError as exc:
                results[tag.tag_id] = self.bad(tag, Quality.BAD_CONFIG_ERROR, "MODBUS_ADDRESS", str(exc))
                continue
            groups[(addr.unit_id, addr.table)].append((addr.offset, addr.count, tag))

        for (unit, table), items in groups.items():
            span = self.conn.max_bits_per_read if table in BIT_TABLES else self.conn.max_registers_per_read
            for start, count, block_tags in coalesce_ranges(items, max_gap=self.conn.max_gap, max_span=span):
                t0 = time.perf_counter()
                try:
                    response = await self._request(table, unit, start, count)
                except ModbusException as exc:
                    for tag in block_tags:
                        results[tag.tag_id] = self.bad(tag, Quality.BAD_DEVICE_FAILURE, "MODBUS_ERROR", str(exc))
                    continue
                duration = (time.perf_counter() - t0) * 1000
                if response.isError():
                    code = getattr(response, "exception_code", None)
                    name = EXCEPTION_NAMES.get(code or 0, f"EXC_{code}")
                    quality = Quality.BAD_ADDRESS if code in (1, 2) else Quality.BAD_DEVICE_FAILURE
                    if len(block_tags) > 1 and code == 2:
                        # isolate the offending address
                        for tag in block_tags:
                            results[tag.tag_id] = await self._read_single(tag)
                        continue
                    for tag in block_tags:
                        results[tag.tag_id] = self.bad(tag, quality, f"MODBUS_{name}", f"exception {code}", duration)
                    continue
                data = response.bits if table in BIT_TABLES else response.registers
                ts = utc_now()
                for tag in block_tags:
                    results[tag.tag_id] = self._decode(tag, data, start, ts, duration)
        return [results[t.tag_id] for t in tags]

    async def _read_single(self, tag: TagConfig) -> RawReadResult:
        addr = self._addr(tag)
        t0 = time.perf_counter()
        response = await self._request(addr.table, addr.unit_id, addr.offset, addr.count)
        duration = (time.perf_counter() - t0) * 1000
        if response.isError():
            code = getattr(response, "exception_code", None)
            name = EXCEPTION_NAMES.get(code or 0, f"EXC_{code}")
            quality = Quality.BAD_ADDRESS if code in (1, 2) else Quality.BAD_DEVICE_FAILURE
            return self.bad(tag, quality, f"MODBUS_{name}", f"exception {code}", duration)
        data = response.bits if addr.is_bit_table else response.registers
        return self._decode(tag, data, addr.offset, utc_now(), duration)

    def _decode(self, tag: TagConfig, data: Sequence[Any], base: int, ts: Any, duration: float) -> RawReadResult:
        addr = self._addr(tag)
        off = addr.offset - base
        try:
            if addr.is_bit_table:
                bits = [bool(b) for b in data[off: off + addr.count]]
                if len(bits) != addr.count:
                    raise EncodingError("short bit response")
                value: Any = bits if tag.array_length else bits[0]
            elif addr.bit is not None:
                value = bool((data[off] >> addr.bit) & 1)
            else:
                regs = list(data[off: off + addr.count])
                if len(regs) != addr.count:
                    raise EncodingError("short register response")
                if tag.array_length:
                    per = addr.count // tag.array_length
                    value = [decode_registers(regs[i * per:(i + 1) * per], tag.data_type,
                                              byte_order=self.conn.byte_order, word_order=self.conn.word_order,
                                              string_length=tag.string_length)
                             for i in range(tag.array_length)]
                else:
                    value = decode_registers(regs, tag.data_type, byte_order=self.conn.byte_order,
                                             word_order=self.conn.word_order, string_length=tag.string_length)
        except (EncodingError, IndexError, UnicodeDecodeError) as exc:
            return self.bad(tag, Quality.BAD_TYPE_MISMATCH, "MODBUS_DECODE", str(exc), duration)
        return RawReadResult(tag.tag_id, value, Quality.GOOD, ts, "gateway_read", duration_ms=duration)

    # ------------------------------------------------------------------ writes
    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        addr = self._addr(tag)
        if addr.table in ("input", "discrete") or addr.bit is not None:
            return self.write_failed(tag, "MODBUS_READ_ONLY", f"{addr.table} address is not writable")
        client = self._require()
        t0 = time.perf_counter()
        try:
            if addr.table == "coil":
                if tag.array_length:
                    response = await client.write_coils(addr.offset, [bool(v) for v in value],  # type: ignore[union-attr]
                                                        device_id=addr.unit_id)
                else:
                    response = await client.write_coil(addr.offset, bool(value), device_id=addr.unit_id)
            else:
                values = value if tag.array_length else [value]
                regs: list[int] = []
                for v in values:  # type: ignore[union-attr]
                    regs += encode_registers(v, tag.data_type, byte_order=self.conn.byte_order,
                                             word_order=self.conn.word_order, string_length=tag.string_length)
                response = await client.write_registers(addr.offset, regs, device_id=addr.unit_id)
        except EncodingError as exc:
            return self.write_failed(tag, "MODBUS_ENCODE", str(exc))
        except (ConnectionException, ModbusIOException, OSError, TimeoutError) as exc:
            return self.write_failed(tag, "MODBUS_CONNECTION", str(exc), (time.perf_counter() - t0) * 1000,
                                     uncertain=True)
        duration = (time.perf_counter() - t0) * 1000
        if response.isError():
            code = getattr(response, "exception_code", None)
            return self.write_failed(tag, f"MODBUS_{EXCEPTION_NAMES.get(code or 0, code)}", f"exception {code}",
                                     duration)
        return self.write_ok(tag, duration)

    async def get_device_info(self) -> DeviceInfo:
        return DeviceInfo(self.device_id, self.driver_name, extra={"endpoint": self._endpoint(),
                                                                   "unit_id": self.conn.unit_id})

    async def health_check(self) -> DriverHealth:
        connected = self.is_connected()
        return DriverHealth(self.device_id, connected, connected,
                            detail=None if connected else "not connected", checked_at=utc_now())
