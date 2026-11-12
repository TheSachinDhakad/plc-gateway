"""16-bit register codec with configurable byte order and word order.

Terminology (value 0x11223344 stored in two registers):

=============  ===========  ============  ==========
byte_order     word_order   registers     name
=============  ===========  ============  ==========
big            big          1122 3344     ABCD (Modbus standard)
little         big          2211 4433     BADC
big            little       3344 1122     CDAB (common on PLCs/meters)
little         little       4433 2211     DCBA
=============  ===========  ============  ==========

Strings: characters are packed two per register following ``byte_order``; ``word_order``
does not apply to strings. Trailing NUL bytes are stripped on decode.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any, Literal

from drivers.codecs.datatypes import DATA_TYPES, DataType, ValueKind
from utils.exceptions import EncodingError

Order = Literal["big", "little"]

REGISTER_TYPES = frozenset({
    DataType.INT, DataType.UINT, DataType.WORD,
    DataType.DINT, DataType.UDINT, DataType.DWORD,
    DataType.LINT, DataType.ULINT, DataType.LWORD,
    DataType.REAL, DataType.LREAL, DataType.STRING,
})


def register_count(data_type: DataType, string_length: int | None = None) -> int:
    if data_type is DataType.STRING:
        length = string_length or 0
        if length <= 0:
            raise EncodingError("STRING over registers requires string_length")
        return (length + 1) // 2
    if data_type not in REGISTER_TYPES:
        raise EncodingError(f"{data_type} cannot be mapped onto 16-bit registers")
    size = DATA_TYPES[data_type].size
    assert size is not None
    return size // 2


def _registers_to_bytes(registers: Sequence[int], byte_order: Order, word_order: Order,
                        apply_word_order: bool) -> bytes:
    regs = list(registers)
    if apply_word_order and word_order == "little":
        regs.reverse()
    out = bytearray()
    for reg in regs:
        if not 0 <= reg <= 0xFFFF:
            raise EncodingError(f"register value {reg} out of 16-bit range")
        hi, lo = reg >> 8, reg & 0xFF
        out += bytes((hi, lo)) if byte_order == "big" else bytes((lo, hi))
    return bytes(out)


def _bytes_to_registers(data: bytes, byte_order: Order, word_order: Order,
                        apply_word_order: bool) -> list[int]:
    if len(data) % 2:
        data += b"\x00"
    regs = []
    for i in range(0, len(data), 2):
        a, b = data[i], data[i + 1]
        regs.append((a << 8 | b) if byte_order == "big" else (b << 8 | a))
    if apply_word_order and word_order == "little":
        regs.reverse()
    return regs


def decode_registers(
    registers: Sequence[int],
    data_type: DataType,
    *,
    byte_order: Order = "big",
    word_order: Order = "big",
    string_length: int | None = None,
) -> Any:
    count = register_count(data_type, string_length)
    if len(registers) != count:
        raise EncodingError(f"{data_type} needs {count} registers, got {len(registers)}")
    if data_type is DataType.STRING:
        raw = _registers_to_bytes(registers, byte_order, word_order, apply_word_order=False)
        raw = raw[: string_length or len(raw)]
        return raw.split(b"\x00", 1)[0].decode("latin-1")
    info = DATA_TYPES[data_type]
    raw = _registers_to_bytes(registers, byte_order, word_order, apply_word_order=True)
    assert info.struct_code is not None
    (value,) = struct.unpack(">" + info.struct_code, raw)
    return value


def encode_registers(
    value: Any,
    data_type: DataType,
    *,
    byte_order: Order = "big",
    word_order: Order = "big",
    string_length: int | None = None,
) -> list[int]:
    count = register_count(data_type, string_length)
    if data_type is DataType.STRING:
        if not isinstance(value, str):
            raise EncodingError("STRING requires str")
        raw = value.encode("latin-1")
        if len(raw) > (string_length or 0):
            raise EncodingError("string exceeds string_length")
        raw = raw.ljust(count * 2, b"\x00")
        return _bytes_to_registers(raw, byte_order, word_order, apply_word_order=False)
    info = DATA_TYPES[data_type]
    assert info.struct_code is not None
    if info.kind is ValueKind.FLOAT:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise EncodingError(f"{data_type} requires a finite number")
    elif isinstance(value, bool) or not isinstance(value, int):
        raise EncodingError(f"{data_type} requires int")
    try:
        raw = struct.pack(">" + info.struct_code, value)
    except (struct.error, OverflowError) as exc:
        raise EncodingError(f"value {value!r} out of range for {data_type}") from exc
    return _bytes_to_registers(raw, byte_order, word_order, apply_word_order=True)
