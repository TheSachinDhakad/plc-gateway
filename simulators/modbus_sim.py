"""Minimal, independent Modbus TCP server (FC 1, 2, 3, 4, 5, 6, 15, 16).

Written from the Modbus Application Protocol spec so the pymodbus *client* is tested against
a separate implementation (pymodbus' own datastore API is deprecated in 3.16).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
import time

TABLE_SIZE = 65536


class ModbusSimulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 1502, units: frozenset[int] = frozenset({1})) -> None:
        self.host = host
        self.port = port
        self.units = set(units)
        self.holding = [0] * TABLE_SIZE
        self.input = [0] * TABLE_SIZE
        self.coils = [False] * TABLE_SIZE
        self.discrete = [False] * TABLE_SIZE
        self.limit = TABLE_SIZE          # addresses >= limit raise ILLEGAL_DATA_ADDRESS
        self.requests = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._animator: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ helpers
    def set_float(self, table: list[int], address: int, value: float) -> None:
        hi, lo = struct.unpack(">HH", struct.pack(">f", value))
        table[address], table[address + 1] = hi, lo

    def _pdu(self, fc: int, data: bytes) -> bytes:
        def exc(code: int) -> bytes:
            return bytes((fc | 0x80, code))

        if fc in (1, 2, 3, 4):
            if len(data) != 4:
                return exc(3)
            start, qty = struct.unpack(">HH", data)
            limit = 2000 if fc in (1, 2) else 125
            if not 1 <= qty <= limit:
                return exc(3)
            if start + qty > self.limit:
                return exc(2)
            if fc in (1, 2):
                bits = (self.coils if fc == 1 else self.discrete)[start:start + qty]
                out = bytearray((qty + 7) // 8)
                for i, b in enumerate(bits):
                    if b:
                        out[i // 8] |= 1 << (i % 8)
                return bytes((fc, len(out))) + bytes(out)
            regs = (self.holding if fc == 3 else self.input)[start:start + qty]
            return bytes((fc, qty * 2)) + struct.pack(f">{qty}H", *regs)
        if fc == 5:
            addr, value = struct.unpack(">HH", data)
            if value not in (0xFF00, 0x0000):
                return exc(3)
            if addr >= self.limit:
                return exc(2)
            self.coils[addr] = value == 0xFF00
            return bytes((fc,)) + data
        if fc == 6:
            addr, value = struct.unpack(">HH", data)
            if addr >= self.limit:
                return exc(2)
            self.holding[addr] = value
            return bytes((fc,)) + data
        if fc in (15, 16):
            addr, qty, count = struct.unpack(">HHB", data[:5])
            payload = data[5:]
            if addr + qty > self.limit:
                return exc(2)
            if fc == 15:
                if count != (qty + 7) // 8 or len(payload) != count:
                    return exc(3)
                for i in range(qty):
                    self.coils[addr + i] = bool(payload[i // 8] >> (i % 8) & 1)
            else:
                if count != qty * 2 or len(payload) != count:
                    return exc(3)
                for i, v in enumerate(struct.unpack(f">{qty}H", payload)):
                    self.holding[addr + i] = v
            return bytes((fc,)) + struct.pack(">HH", addr, qty)
        return exc(1)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                body = await reader.readexactly(length - 1)
                self.requests += 1
                if pid != 0:
                    continue
                fc, data = body[0], body[1:]
                pdu = self._pdu(fc, data) if unit in self.units else bytes((fc | 0x80, 0x0B))
                writer.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]

    async def animate(self, period_s: float = 1.0) -> None:
        """Change values like a running process: REAL sine at holding 0, counter at input 100."""
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            self.set_float(self.holding, 0, 230.0 + 5 * math.sin(t / 10))
            counter = int(t) & 0xFFFFFFFF
            self.input[100], self.input[101] = counter >> 16, counter & 0xFFFF
            self.discrete[0] = int(t / 30) % 2 == 0
            await asyncio.sleep(period_s)

    def start_animation(self) -> None:
        self._animator = asyncio.create_task(self.animate())

    async def stop(self) -> None:
        if self._animator:
            self._animator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._animator
        if self._server:
            self._server.close()
            for w in list(self._writers):   # Python 3.11 keeps accepted connections open
                w.close()
            await self._server.wait_closed()
