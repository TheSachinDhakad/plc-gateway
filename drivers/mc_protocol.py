"""Mitsubishi MC protocol framing — binary 3E (SLMP compatible) and binary 1E frames.

Pure functions (no I/O) so every frame can be unit-tested and reused by the simulator.

3E binary request::

    50 00 | net | pc | io(2,LE) | station | len(2,LE) | timer(2,LE) | cmd(2,LE) | sub(2,LE) | data
    (len counts from timer to end)

3E binary response::

    D0 00 | net | pc | io(2) | station | len(2,LE) | end_code(2,LE) | data

Device specification: Q/L/FX5 = number(3,LE) + code(1); iQ-R (sub 0x0002/0x0003) =
number(4,LE) + code(2,LE).

1E binary request (FX3U-ENET-ADP / FX3U-ENET)::

    cmd | pc | timer(2,LE) | head_no(4,LE) | dev_code(2,LE) | points(1, 0=256) | 00 | data

1E binary response::

    cmd|0x80 | completion | [abnormal_code if completion == 0x5B] | data

Bit data is packed 2 points per byte, high nibble first (1 = ON).
Word data is 2 bytes little-endian per point.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

from utils.exceptions import ProtocolError

# 3E commands
CMD_BATCH_READ = 0x0401
CMD_BATCH_WRITE = 0x1401
CMD_READ_CPU_MODEL = 0x0101
SUB_WORD = 0x0000
SUB_BIT = 0x0001
SUB_WORD_IQR = 0x0002
SUB_BIT_IQR = 0x0003

# 1E commands
CMD1E_READ_BIT = 0x00
CMD1E_READ_WORD = 0x01
CMD1E_WRITE_BIT = 0x02
CMD1E_WRITE_WORD = 0x03

HEADER_3E_REQ = b"\x50\x00"
HEADER_3E_RES = b"\xd0\x00"
RESPONSE_3E_HEADER_LEN = 9


@dataclass(frozen=True, slots=True)
class Route3E:
    network_no: int = 0
    pc_no: int = 0xFF
    dest_module_io: int = 0x03FF
    dest_module_station: int = 0


class MCEndCodeError(Exception):
    """Device returned a non-zero end/completion code (connection still usable)."""

    def __init__(self, end_code: int, detail: str = "") -> None:
        super().__init__(f"MC end code 0x{end_code:04X}{(' ' + detail) if detail else ''}")
        self.end_code = end_code


# --------------------------------------------------------------------------- bit packing


def pack_bits(values: Sequence[bool]) -> bytes:
    out = bytearray()
    for i in range(0, len(values), 2):
        hi = 0x10 if values[i] else 0
        lo = 0x01 if i + 1 < len(values) and values[i + 1] else 0
        out.append(hi | lo)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[bool]:
    if len(data) < (count + 1) // 2:
        raise ProtocolError("bit response too short")
    out = []
    for i in range(count):
        byte = data[i // 2]
        out.append(bool(byte & 0x10) if i % 2 == 0 else bool(byte & 0x01))
    return out


def pack_words(words: Sequence[int]) -> bytes:
    return b"".join(struct.pack("<H", w & 0xFFFF) for w in words)


def unpack_words(data: bytes, count: int) -> list[int]:
    if len(data) < count * 2:
        raise ProtocolError("word response too short")
    return list(struct.unpack_from(f"<{count}H", data))


# --------------------------------------------------------------------------- 3E frames


def device_spec_3e(code: int, number: int, iqr: bool) -> bytes:
    if iqr:
        return struct.pack("<IH", number, code)
    if number > 0xFFFFFF:
        raise ProtocolError("device number exceeds 3 bytes")
    return struct.pack("<I", number)[:3] + bytes((code & 0xFF,))


def build_3e(command: int, subcommand: int, payload: bytes, route: Route3E, timer_units: int) -> bytes:
    body = struct.pack("<HHH", timer_units, command, subcommand) + payload
    return (HEADER_3E_REQ
            + struct.pack("<BBHB", route.network_no, route.pc_no, route.dest_module_io, route.dest_module_station)
            + struct.pack("<H", len(body)) + body)


def build_3e_batch_read(code: int, number: int, points: int, *, bit_units: bool, iqr: bool,
                        route: Route3E, timer_units: int) -> bytes:
    sub = (SUB_BIT_IQR if iqr else SUB_BIT) if bit_units else (SUB_WORD_IQR if iqr else SUB_WORD)
    payload = device_spec_3e(code, number, iqr) + struct.pack("<H", points)
    return build_3e(CMD_BATCH_READ, sub, payload, route, timer_units)


def build_3e_batch_write(code: int, number: int, *, bits: Sequence[bool] | None = None,
                         words: Sequence[int] | None = None, iqr: bool, route: Route3E,
                         timer_units: int) -> bytes:
    if (bits is None) == (words is None):
        raise ValueError("exactly one of bits/words required")
    if bits is not None:
        sub = SUB_BIT_IQR if iqr else SUB_BIT
        payload = device_spec_3e(code, number, iqr) + struct.pack("<H", len(bits)) + pack_bits(bits)
    else:
        assert words is not None
        sub = SUB_WORD_IQR if iqr else SUB_WORD
        payload = device_spec_3e(code, number, iqr) + struct.pack("<H", len(words)) + pack_words(words)
    return build_3e(CMD_BATCH_WRITE, sub, payload, route, timer_units)


def parse_3e_response_header(header: bytes) -> int:
    """Validate the 9-byte response header and return the remaining length."""
    if len(header) != RESPONSE_3E_HEADER_LEN or header[:2] != HEADER_3E_RES:
        raise ProtocolError(f"invalid 3E response header {header.hex()}")
    (length,) = struct.unpack_from("<H", header, 7)
    if length < 2:
        raise ProtocolError("3E response length too small")
    return length


def parse_3e_response_body(body: bytes) -> bytes:
    (end_code,) = struct.unpack_from("<H", body, 0)
    if end_code != 0:
        raise MCEndCodeError(end_code, body[2:].hex())
    return body[2:]


# --------------------------------------------------------------------------- 1E frames


def build_1e(command: int, pc_no: int, timer_units: int, code: int, number: int, points: int,
             data: bytes = b"") -> bytes:
    if not 1 <= points <= 256:
        raise ProtocolError("1E points must be 1..256")
    return struct.pack("<BBHIHBB", command, pc_no, timer_units, number, code, points % 256, 0) + data


def build_1e_batch_read(code: int, number: int, points: int, *, bit_units: bool, pc_no: int,
                        timer_units: int) -> bytes:
    return build_1e(CMD1E_READ_BIT if bit_units else CMD1E_READ_WORD, pc_no, timer_units, code, number, points)


def build_1e_batch_write(code: int, number: int, *, bits: Sequence[bool] | None = None,
                         words: Sequence[int] | None = None, pc_no: int, timer_units: int) -> bytes:
    if bits is not None:
        return build_1e(CMD1E_WRITE_BIT, pc_no, timer_units, code, number, len(bits), pack_bits(bits))
    assert words is not None
    return build_1e(CMD1E_WRITE_WORD, pc_no, timer_units, code, number, len(words), pack_words(words))


def expected_1e_data_len(command: int, points: int) -> int:
    if command == CMD1E_READ_BIT:
        return (points + 1) // 2
    if command == CMD1E_READ_WORD:
        return points * 2
    return 0


def check_1e_response_head(head: bytes, command: int) -> int:
    """Validate the 2-byte head; return completion code."""
    if len(head) != 2 or head[0] != (command | 0x80):
        raise ProtocolError(f"invalid 1E response head {head.hex()} for command 0x{command:02X}")
    return head[1]
