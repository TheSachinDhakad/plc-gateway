"""Mitsubishi MC protocol driver (FX5U/Q/L/iQ-R via 3E binary, FX3U via 1E binary).

Native asyncio TCP implementation built on :mod:`drivers.mc_protocol` (pymcprotocol is
unmaintained and lacks the 1E frame). One request is in flight per connection.

PLC-side setup (GX Works2/3): an MC/SLMP connection on TCP with **binary** code, the port
configured in ``connection.port`` and — only if writes are required — "Enable online change
(write during RUN)". Multi-word values use Mitsubishi little-endian word order
(low word at the lower device number). Strings are 2 chars per word, low byte first.

Hardware validation required before production use: FX3U 1E frame point limits and FX5U
octal X/Y numbering in binary SLMP frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from drivers import mc_protocol as mc
from drivers.base import BaseDriver, coalesce_ranges
from drivers.codecs.datatypes import DATA_TYPES, DataType, ValueKind
from drivers.codecs.mitsubishi_address import MCAddress, parse_mc_address
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, MCConnection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagAccess, TagConfig
from utils.exceptions import AddressError, DriverConnectionError, EncodingError, ProtocolError, TagConfigError
from utils.timeutil import utc_now

log = logging.getLogger(__name__)

MC_WORD_TYPES = frozenset({
    DataType.INT, DataType.UINT, DataType.WORD, DataType.DINT, DataType.UDINT, DataType.DWORD,
    DataType.LINT, DataType.ULINT, DataType.LWORD, DataType.REAL, DataType.LREAL, DataType.STRING,
})


@dataclass(frozen=True, slots=True)
class _Plan:
    tag: TagConfig
    addr: MCAddress
    bit_units: bool
    points: int


def _word_count(tag: TagConfig) -> int:
    if tag.data_type is DataType.STRING:
        if not tag.string_length:
            raise TagConfigError("STRING on Mitsubishi requires string_length")
        return (tag.string_length + 1) // 2
    size = DATA_TYPES[tag.data_type].size
    assert size is not None
    return max(1, size // 2) * (tag.array_length or 1)


def plan_tag(tag: TagConfig, series: str) -> _Plan:
    addr = parse_mc_address(tag.address, series)
    if addr.is_bit_device:
        if tag.data_type is not DataType.BOOL:
            raise TagConfigError(f"bit device {addr.device} requires data_type BOOL")
        return _Plan(tag, addr, True, tag.array_length or 1)
    if tag.data_type is DataType.BOOL:
        if addr.bit is None:
            raise TagConfigError("BOOL on a word device requires a bit suffix, e.g. D100.3")
        if tag.array_length:
            raise TagConfigError("arrays of word bits are not supported")
        return _Plan(tag, addr, False, 1)
    if addr.bit is not None:
        raise TagConfigError("bit suffix requires data_type BOOL")
    if tag.data_type not in MC_WORD_TYPES:
        raise TagConfigError(f"data type {tag.data_type} not supported on Mitsubishi word devices")
    if tag.data_type is DataType.STRING and tag.array_length:
        raise TagConfigError("arrays of STRING are not supported")
    return _Plan(tag, addr, False, _word_count(tag))


def decode_words(words: Sequence[int], tag: TagConfig, bit: int | None) -> Any:
    if tag.data_type is DataType.BOOL:
        assert bit is not None
        return bool((words[0] >> bit) & 1)
    raw = mc.pack_words(words)
    if tag.data_type is DataType.STRING:
        return raw[: tag.string_length].split(b"\x00", 1)[0].decode("latin-1")
    info = DATA_TYPES[tag.data_type]
    assert info.size is not None and info.struct_code is not None
    count = tag.array_length or 1
    size = max(info.size, 2)
    values = []
    for i in range(count):
        chunk = raw[i * size: (i + 1) * size]
        (v,) = struct.unpack("<" + info.struct_code, chunk[: info.size])
        values.append(v)
    return values if tag.array_length else values[0]


def encode_words(value: Any, tag: TagConfig) -> list[int]:
    if tag.data_type is DataType.STRING:
        raw = str(value).encode("latin-1")
        if len(raw) > (tag.string_length or 0):
            raise EncodingError("string exceeds string_length")
        raw = raw.ljust(_word_count(tag) * 2, b"\x00")
        return list(struct.unpack(f"<{len(raw) // 2}H", raw))
    info = DATA_TYPES[tag.data_type]
    assert info.struct_code is not None
    values = value if tag.array_length else [value]
    raw = b""
    for v in values:
        if info.kind is ValueKind.INT and (isinstance(v, bool) or not isinstance(v, int)):
            raise EncodingError(f"{tag.data_type} requires int")
        try:
            raw += struct.pack("<" + info.struct_code, v)
        except (struct.error, OverflowError) as exc:
            raise EncodingError(f"value {v!r} out of range for {tag.data_type}") from exc
    return list(struct.unpack(f"<{len(raw) // 2}H", raw))


class MitsubishiMCDriver(BaseDriver):
    driver_name = "mitsubishi_mc"
    supports_write = True

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, MCConnection)
        self.conn: MCConnection = device.connection
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._plans: dict[str, _Plan] = {}
        self._iqr = self.conn.plc_series == "iQ-R"
        self._route = mc.Route3E(self.conn.network_no, self.conn.pc_no, self.conn.dest_module_io,
                                 self.conn.dest_module_station)
        self._timer = max(1, self.conn.monitoring_timer_ms // 250)

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        conn = device.connection
        assert isinstance(conn, MCConnection)
        try:
            plan = plan_tag(tag, conn.plc_series)
            if conn.frame == "1E":
                plan.addr.code_1e()
        except AddressError as exc:
            raise TagConfigError(str(exc)) from exc
        limit = conn.bit_limit if plan.bit_units else conn.word_limit
        if plan.points > limit:
            raise TagConfigError(f"tag needs {plan.points} points, exceeds frame limit {limit}")
        if tag.access is TagAccess.READ_WRITE and not plan.addr.is_bit_device and plan.addr.bit is not None:
            raise TagConfigError("writing single bits of word devices is not supported (no read-modify-write)")

    def _plan(self, tag: TagConfig) -> _Plan:
        plan = self._plans.get(tag.tag_id)
        if plan is None:
            plan = plan_tag(tag, self.conn.plc_series)
            self._plans[tag.tag_id] = plan
        return plan

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.conn.host, self.conn.port), self.conn.timeout_s)
        except (OSError, TimeoutError) as exc:
            raise DriverConnectionError(
                f"{self.device_id}: MC connect {self.conn.host}:{self.conn.port} failed: {exc}") from exc
        log.info("MC connected (%s frame, %s)", self.conn.frame, self.conn.plc_series,
                 extra={"device_id": self.device_id, "operation": "connect"})

    async def disconnect(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)

    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _io(self, frame: bytes, read_fn: Any) -> Any:
        if self._writer is None or self._reader is None:
            raise DriverConnectionError(f"{self.device_id}: not connected")
        async with self._lock:
            try:
                self._writer.write(frame)
                await self._writer.drain()
                return await asyncio.wait_for(read_fn(self._reader), self.conn.timeout_s)
            except mc.MCEndCodeError:
                raise
            except (OSError, TimeoutError, asyncio.IncompleteReadError, ProtocolError) as exc:
                await self.disconnect()   # stream state unknown after a failed exchange
                raise DriverConnectionError(f"{self.device_id}: MC exchange failed: {exc!r}") from exc

    async def _transact_3e(self, frame: bytes) -> bytes:
        async def read(reader: asyncio.StreamReader) -> bytes:
            header = await reader.readexactly(mc.RESPONSE_3E_HEADER_LEN)
            length = mc.parse_3e_response_header(header)
            return await reader.readexactly(length)

        body = await self._io(frame, read)
        return mc.parse_3e_response_body(body)

    async def _transact_1e(self, frame: bytes, command: int, points: int) -> bytes:
        async def read(reader: asyncio.StreamReader) -> tuple[int, int | None, bytes]:
            head = await reader.readexactly(2)
            completion = mc.check_1e_response_head(head, command)
            if completion == 0:
                return 0, None, await reader.readexactly(mc.expected_1e_data_len(command, points))
            if completion == 0x5B:
                return completion, (await reader.readexactly(1))[0], b""
            return completion, None, b""

        completion, abnormal, data = await self._io(frame, read)
        if completion != 0:
            raise mc.MCEndCodeError(completion, f"abnormal=0x{abnormal:02X}" if abnormal is not None else "")
        return data

    async def _read_block(self, addr: MCAddress, number: int, points: int, bit_units: bool) -> list[Any]:
        if self.conn.frame == "1E":
            cmd = mc.CMD1E_READ_BIT if bit_units else mc.CMD1E_READ_WORD
            frame = mc.build_1e_batch_read(addr.code_1e(), number, points, bit_units=bit_units,
                                           pc_no=self.conn.pc_no, timer_units=self._timer)
            data = await self._transact_1e(frame, cmd, points)
        else:
            frame = mc.build_3e_batch_read(addr.code_3e(), number, points, bit_units=bit_units, iqr=self._iqr,
                                           route=self._route, timer_units=self._timer)
            data = await self._transact_3e(frame)
        return mc.unpack_bits(data, points) if bit_units else mc.unpack_words(data, points)

    # ------------------------------------------------------------------ reads
    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        results: dict[str, RawReadResult] = {}
        groups: dict[tuple[str, bool], list[tuple[int, int, _Plan]]] = defaultdict(list)
        for tag in tags:
            try:
                plan = self._plan(tag)
            except (AddressError, TagConfigError) as exc:
                results[tag.tag_id] = self.bad(tag, Quality.BAD_CONFIG_ERROR, "MC_ADDRESS", str(exc))
                continue
            groups[(plan.addr.device, plan.bit_units)].append((plan.addr.number, plan.points, plan))

        for (_device, bit_units), items in groups.items():
            span = self.conn.bit_limit if bit_units else self.conn.word_limit
            for start, length, plans in coalesce_ranges(items, max_gap=self.conn.max_gap, max_span=span):
                t0 = time.perf_counter()
                try:
                    values = await self._read_block(plans[0].addr, start, length, bit_units)
                except mc.MCEndCodeError as exc:
                    duration = (time.perf_counter() - t0) * 1000
                    for p in plans:
                        results[p.tag.tag_id] = self.bad(p.tag, Quality.BAD_DEVICE_FAILURE,
                                                         f"MC_END_0x{exc.end_code:04X}", str(exc), duration)
                    continue
                duration = (time.perf_counter() - t0) * 1000
                ts = utc_now()
                for p in plans:
                    off = p.addr.number - start
                    chunk = values[off: off + p.points]
                    try:
                        if bit_units:
                            value: Any = list(chunk) if p.tag.array_length else chunk[0]
                        else:
                            value = decode_words(chunk, p.tag, p.addr.bit)
                        results[p.tag.tag_id] = RawReadResult(p.tag.tag_id, value, Quality.GOOD, ts,
                                                              "gateway_read", duration_ms=duration)
                    except (struct.error, EncodingError, UnicodeDecodeError) as exc:
                        results[p.tag.tag_id] = self.bad(p.tag, Quality.BAD_TYPE_MISMATCH, "MC_DECODE", str(exc))
        return [results[t.tag_id] for t in tags]

    # ------------------------------------------------------------------ writes
    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        t0 = time.perf_counter()
        try:
            plan = self._plan(tag)
            if not plan.addr.is_bit_device and plan.addr.bit is not None:
                return self.write_failed(tag, "MC_UNSUPPORTED", "word-bit writes are not supported")
            if plan.bit_units:
                bits = [bool(v) for v in value] if isinstance(value, list) else [bool(value)]
                if self.conn.frame == "1E":
                    frame = mc.build_1e_batch_write(plan.addr.code_1e(), plan.addr.number, bits=bits,
                                                    pc_no=self.conn.pc_no, timer_units=self._timer)
                else:
                    frame = mc.build_3e_batch_write(plan.addr.code_3e(), plan.addr.number, bits=bits,
                                                    iqr=self._iqr, route=self._route, timer_units=self._timer)
                cmd = mc.CMD1E_WRITE_BIT
            else:
                words = encode_words(value, tag)
                if self.conn.frame == "1E":
                    frame = mc.build_1e_batch_write(plan.addr.code_1e(), plan.addr.number, words=words,
                                                    pc_no=self.conn.pc_no, timer_units=self._timer)
                else:
                    frame = mc.build_3e_batch_write(plan.addr.code_3e(), plan.addr.number, words=words,
                                                    iqr=self._iqr, route=self._route, timer_units=self._timer)
                cmd = mc.CMD1E_WRITE_WORD
        except (EncodingError, AddressError, TagConfigError) as exc:
            return self.write_failed(tag, "MC_ENCODE", str(exc))
        try:
            if self.conn.frame == "1E":
                await self._transact_1e(frame, cmd, plan.points)
            else:
                await self._transact_3e(frame)
        except mc.MCEndCodeError as exc:
            return self.write_failed(tag, f"MC_END_0x{exc.end_code:04X}", str(exc), (time.perf_counter() - t0) * 1000)
        except DriverConnectionError as exc:
            return self.write_failed(tag, "MC_CONNECTION", str(exc), (time.perf_counter() - t0) * 1000,
                                     uncertain=True)
        return self.write_ok(tag, (time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------------------ info / health
    async def _read_cpu_model(self) -> tuple[str, int]:
        frame = mc.build_3e(mc.CMD_READ_CPU_MODEL, 0x0000, b"", self._route, self._timer)
        data = await self._transact_3e(frame)
        if len(data) < 18:
            raise ProtocolError("CPU model response too short")
        return data[:16].decode("ascii", "replace").strip(), struct.unpack_from("<H", data, 16)[0]

    async def get_device_info(self) -> DeviceInfo:
        info = DeviceInfo(self.device_id, self.driver_name, vendor="Mitsubishi Electric",
                          model=self.conn.plc_series, extra={"frame": self.conn.frame})
        if self.conn.frame == "3E":
            try:
                name, code = await self._read_cpu_model()
                info.model = name
                info.extra["model_code"] = f"0x{code:04X}"
            except mc.MCEndCodeError as exc:
                info.extra["cpu_model_error"] = str(exc)
        return info

    async def health_check(self) -> DriverHealth:
        t0 = time.perf_counter()
        if not self.is_connected():
            return DriverHealth(self.device_id, False, False, detail="not connected", checked_at=utc_now())
        try:
            if self.conn.frame == "3E":
                await self._read_cpu_model()
            else:
                await self._read_block(parse_mc_address("D0", self.conn.plc_series), 0, 1, False)
            return DriverHealth(self.device_id, True, True, (time.perf_counter() - t0) * 1000, None, utc_now())
        except Exception as exc:
            return DriverHealth(self.device_id, self.is_connected(), False, detail=str(exc), checked_at=utc_now())
