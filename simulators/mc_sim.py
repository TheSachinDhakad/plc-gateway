"""Mitsubishi MC protocol simulator: binary 3E (Q/L/FX5/iQ-R) and binary 1E (FX3U-ENET-ADP).

Request frame type is detected from the first byte (0x50 = 3E subheader, else 1E command).
Device memory is shared between frame types. Unknown devices/addresses return end codes.
"""

from __future__ import annotations

import asyncio
import struct

from drivers import mc_protocol as mc
from drivers.codecs.mitsubishi_address import DEVICES

SIZE = 65536
END_DEVICE_ERROR = 0xC056
END_COMMAND_ERROR = 0xC059
END_1E_ERROR = 0x5B

_CODE_3E = {code3: name for name, (_, code3, _) in DEVICES.items()}
_CODE_1E = {code1: name for name, (_, _, code1) in DEVICES.items() if code1 is not None}


class MCSimulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 5000, cpu_model: str = "FX5U-32MT/ES") -> None:
        self.host = host
        self.port = port
        self.cpu_model = cpu_model
        self.bits: dict[str, list[bool]] = {n: [False] * SIZE for n, (is_bit, _, _) in DEVICES.items() if is_bit}
        self.words: dict[str, list[int]] = {n: [0] * SIZE for n, (is_bit, _, _) in DEVICES.items() if not is_bit}
        self.requests = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    # ------------------------------------------------------------------ memory access
    def _read(self, device: str, number: int, points: int, bit_units: bool) -> bytes:
        if number + points > SIZE:
            raise IndexError
        if bit_units:
            if device in self.bits:
                return mc.pack_bits(self.bits[device][number:number + points])
            # bit access to a word device: bit n of the word area
            vals = [bool(self.words[device][(number + i) // 16] >> ((number + i) % 16) & 1) for i in range(points)]
            return mc.pack_bits(vals)
        if device in self.words:
            return mc.pack_words(self.words[device][number:number + points])
        # word access to a bit device: 16 bits per word, LSB = lowest device number
        out = []
        for w in range(points):
            bits = self.bits[device][number + w * 16: number + w * 16 + 16]
            out.append(sum(1 << i for i, b in enumerate(bits) if b))
        return mc.pack_words(out)

    def _write(self, device: str, number: int, points: int, bit_units: bool, data: bytes) -> None:
        if number + points > SIZE:
            raise IndexError
        if bit_units:
            values = mc.unpack_bits(data, points)
            self.bits[device][number:number + points] = values
        else:
            values = mc.unpack_words(data, points)
            self.words[device][number:number + points] = values

    # ------------------------------------------------------------------ 3E
    def _handle_3e(self, header: bytes, body: bytes) -> bytes:
        route = header[2:7]
        _timer, cmd, sub = struct.unpack_from("<HHH", body, 0)
        data = body[6:]
        end, payload = 0, b""
        try:
            if cmd == mc.CMD_READ_CPU_MODEL:
                payload = self.cpu_model.encode("ascii")[:16].ljust(16, b" ") + struct.pack("<H", 0x4A21)
            elif cmd in (mc.CMD_BATCH_READ, mc.CMD_BATCH_WRITE):
                iqr = sub in (mc.SUB_WORD_IQR, mc.SUB_BIT_IQR)
                bit_units = sub in (mc.SUB_BIT, mc.SUB_BIT_IQR)
                if iqr:
                    number, code = struct.unpack_from("<IH", data, 0)
                    off = 6
                else:
                    number = int.from_bytes(data[0:3], "little")
                    code = data[3]
                    off = 4
                (points,) = struct.unpack_from("<H", data, off)
                device = _CODE_3E.get(code)
                if device is None:
                    end = END_DEVICE_ERROR
                elif cmd == mc.CMD_BATCH_READ:
                    payload = self._read(device, number, points, bit_units)
                else:
                    self._write(device, number, points, bit_units, data[off + 2:])
            else:
                end = END_COMMAND_ERROR
        except (IndexError, struct.error, KeyError):
            end, payload = END_DEVICE_ERROR, b""
        if end:
            payload = b""
        resp_body = struct.pack("<H", end) + payload
        return mc.HEADER_3E_RES + route + struct.pack("<H", len(resp_body)) + resp_body

    # ------------------------------------------------------------------ 1E
    def _handle_1e(self, head: bytes, data: bytes) -> bytes:
        cmd = head[0]
        _pc, _timer, number, code, points, _ = struct.unpack("<BHIHBB", head[1:])
        points = points or 256
        device = _CODE_1E.get(code)
        try:
            if device is None or cmd not in (0, 1, 2, 3):
                raise KeyError
            bit_units = cmd in (mc.CMD1E_READ_BIT, mc.CMD1E_WRITE_BIT)
            if cmd in (mc.CMD1E_READ_BIT, mc.CMD1E_READ_WORD):
                return bytes((cmd | 0x80, 0)) + self._read(device, number, points, bit_units)
            self._write(device, number, points, bit_units, data)
            return bytes((cmd | 0x80, 0))
        except (IndexError, KeyError, struct.error):
            return bytes((cmd | 0x80, END_1E_ERROR, 0x10))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                first = await reader.readexactly(1)
                self.requests += 1
                if first == b"\x50":
                    header = first + await reader.readexactly(8)
                    (length,) = struct.unpack_from("<H", header, 7)
                    body = await reader.readexactly(length)
                    writer.write(self._handle_3e(header, body))
                else:
                    head = first + await reader.readexactly(11)
                    cmd = head[0]
                    points = head[10] or 256
                    data = b""
                    if cmd == mc.CMD1E_WRITE_BIT:
                        data = await reader.readexactly((points + 1) // 2)
                    elif cmd == mc.CMD1E_WRITE_WORD:
                        data = await reader.readexactly(points * 2)
                    writer.write(self._handle_1e(head, data))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for w in list(self._writers):   # Python 3.11 keeps accepted connections open
                w.close()
            await self._server.wait_closed()
