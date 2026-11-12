"""OPC DA client driver (Windows only) — talks to :mod:`drivers.opc_da_bridge` over a pipe.

This driver refuses to load on macOS/Linux (PlatformNotSupportedError at config validation).
Status: PARTIAL — implemented against the OPC DA Automation 2.0 interface; must be validated
with the actual OPC DA server and wrapper DLL on the target PC (see COMPATIBILITY.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from drivers.base import BaseDriver
from drivers.codecs.datatypes import DataType, python_value_matches
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, OpcDaConnection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagConfig
from utils.exceptions import DriverConnectionError, TagConfigError
from utils.timeutil import from_iso, utc_now

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def map_da_quality(quality: int) -> Quality:
    """OPC DA quality: bits 7-6 = 11 good, 01 uncertain, 00 bad. Sub-status 0x18 = comm failure."""
    major = quality & 0xC0
    if major == 0xC0:
        return Quality.GOOD
    if major == 0x40:
        return Quality.UNCERTAIN
    sub = quality & 0x3C
    if sub == 0x18:
        return Quality.BAD_COMM_FAILURE
    if sub == 0x0C:
        return Quality.BAD_DEVICE_FAILURE
    if sub == 0x04:
        return Quality.BAD_CONFIG_ERROR
    if sub == 0x08:
        return Quality.BAD_NOT_CONNECTED
    return Quality.BAD


class OpcDaDriver(BaseDriver):
    driver_name = "opc_da"
    supports_write = True
    platforms = frozenset({"win32"})

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, OpcDaConnection)
        self.conn: OpcDaConnection = device.connection
        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        if tag.data_type in (DataType.WSTRING, DataType.CHAR):
            raise TagConfigError(f"{tag.data_type} not supported by OPC DA driver; use STRING")
        if not tag.address.strip():
            raise TagConfigError("OPC DA item id must not be empty")

    async def _call(self, op: str, **payload: Any) -> Any:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None or proc.returncode is not None:
            raise DriverConnectionError(f"{self.device_id}: OPC DA bridge not running")
        req_id = next(self._ids)
        async with self._lock:
            try:
                proc.stdin.write((json.dumps({"id": req_id, "op": op, **payload}) + "\n").encode())
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), self.conn.timeout_s)
            except (OSError, TimeoutError, BrokenPipeError) as exc:
                await self._kill()
                raise DriverConnectionError(f"{self.device_id}: OPC DA bridge I/O failed: {exc!r}") from exc
        if not line:
            await self._kill()
            raise DriverConnectionError(f"{self.device_id}: OPC DA bridge exited")
        resp = json.loads(line)
        if resp.get("id") != req_id:
            await self._kill()
            raise DriverConnectionError(f"{self.device_id}: OPC DA bridge response out of sequence")
        if not resp.get("ok"):
            raise DriverConnectionError(f"{self.device_id}: OPC DA error: {resp.get('error')}")
        return resp.get("result")

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)

    async def connect(self) -> None:
        await self.disconnect()
        python = self.conn.bridge_python or sys.executable
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                python, "-m", "drivers.opc_da_bridge", cwd=str(PROJECT_ROOT), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            raise DriverConnectionError(f"{self.device_id}: cannot start OPC DA bridge: {exc}") from exc
        await self._call("connect", server=self.conn.server_progid, host=self.conn.host,
                         automation=self.conn.automation_progid)
        log.info("OPC DA connected to %s on %s", self.conn.server_progid, self.conn.host,
                 extra={"device_id": self.device_id, "operation": "connect"})

    async def disconnect(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            await self._call("disconnect")
        await self._kill()

    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        t0 = time.perf_counter()
        rows = await self._call("read", items=[t.address for t in tags])
        duration = (time.perf_counter() - t0) * 1000
        out = []
        for tag, row in zip(tags, rows, strict=True):
            ts = from_iso(row.get("timestamp")) or utc_now()
            origin = "device" if row.get("timestamp") else "gateway_read"
            if row.get("error"):
                out.append(RawReadResult(tag.tag_id, None, Quality.BAD_ADDRESS, ts, origin, "OPCDA_ITEM_ERROR",
                                         row["error"], duration))
                continue
            quality = map_da_quality(int(row.get("quality", 0)))
            value = row.get("value")
            if quality.is_bad:
                out.append(RawReadResult(tag.tag_id, None, quality, ts, origin, f"OPCDA_Q_{row.get('quality')}",
                                         None, duration))
                continue
            if isinstance(value, int) and tag.data_type in (DataType.REAL, DataType.LREAL):
                value = float(value)
            matches = (isinstance(value, list) and all(python_value_matches(tag.data_type, v) for v in value)
                       if tag.array_length else python_value_matches(tag.data_type, value))
            if not matches:
                out.append(RawReadResult(tag.tag_id, None, Quality.BAD_TYPE_MISMATCH, ts, origin,
                                         "OPCDA_TYPE_MISMATCH", f"got {type(value).__name__}", duration))
                continue
            out.append(RawReadResult(tag.tag_id, value, quality, ts, origin, None, None, duration))
        return out

    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        t0 = time.perf_counter()
        try:
            await self._call("write", item=tag.address, value=value)
        except DriverConnectionError as exc:
            uncertain = not self.is_connected()
            return self.write_failed(tag, "OPCDA_WRITE", str(exc), (time.perf_counter() - t0) * 1000,
                                     uncertain=uncertain)
        return self.write_ok(tag, (time.perf_counter() - t0) * 1000)

    async def get_device_info(self) -> DeviceInfo:
        raw = await self._call("info")
        return DeviceInfo(self.device_id, self.driver_name, vendor=raw.get("vendor"), model=raw.get("server_name"),
                          firmware=raw.get("version"), extra={"server_state": raw.get("server_state")})

    async def health_check(self) -> DriverHealth:
        t0 = time.perf_counter()
        try:
            raw = await self._call("info")
            ok = raw.get("server_state") == 1   # OPC_STATUS_RUNNING
            return DriverHealth(self.device_id, True, ok, (time.perf_counter() - t0) * 1000,
                                f"server_state={raw.get('server_state')}", utc_now())
        except DriverConnectionError as exc:
            return DriverHealth(self.device_id, self.is_connected(), False, detail=str(exc), checked_at=utc_now())
