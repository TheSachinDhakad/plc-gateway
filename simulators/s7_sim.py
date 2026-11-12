"""Siemens S7 simulator built on python-snap7's bundled ``snap7.Server``.

Serves DB100 (non-optimized layout matching config/sim/tags_s7.yaml) and the marker area.
Port 102 needs elevated rights on macOS/Linux, so the simulator defaults to 1102.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
import time

import snap7

DB100_SIZE = 512


class S7Simulator:
    def __init__(self, port: int = 1102, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self.db100 = bytearray(DB100_SIZE)
        self.markers = bytearray(256)
        self._server: snap7.Server | None = None
        self._animator: asyncio.Task[None] | None = None

    def set_real(self, offset: int, value: float) -> None:
        self.db100[offset:offset + 4] = struct.pack(">f", value)

    def set_int(self, offset: int, value: int) -> None:
        self.db100[offset:offset + 2] = struct.pack(">h", value)

    def set_bool(self, offset: int, bit: int, value: bool) -> None:
        if value:
            self.db100[offset] |= 1 << bit
        else:
            self.db100[offset] &= ~(1 << bit) & 0xFF

    def start(self) -> None:
        server = snap7.Server()
        server.register_area(snap7.SrvArea.DB, 100, self.db100)
        server.register_area(snap7.SrvArea.MK, 0, self.markers)
        server.start_to(self.host, tcp_port=self.port)
        self._server = server

    async def animate(self, period_s: float = 1.0) -> None:
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            self.set_real(0, 25.0 + 3 * math.sin(t / 20))        # Temperature
            self.set_real(4, 4.0 + 0.2 * math.cos(t / 15))       # Pressure
            self.set_int(8, int(t) % 32767)                      # Counter
            self.set_bool(10, 0, int(t / 20) % 2 == 0)           # MotorStatus
            await asyncio.sleep(period_s)

    def start_animation(self) -> None:
        self._animator = asyncio.create_task(self.animate())

    async def stop(self) -> None:
        if self._animator:
            self._animator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._animator
        if self._server:
            self._server.stop()
            self._server.destroy()
            self._server = None
