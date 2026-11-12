"""Siemens S7 absolute address parser and big-endian value codec.

Supported address forms (case-insensitive):

* Data blocks:  ``DB100.DBX10.0``  ``DB100.DBB2``  ``DB100.DBW8``  ``DB100.DBD0``
* Markers:      ``M5.1``  ``MB5``  ``MW10``  ``MD20``
* Inputs:       ``I0.0``  ``IB0``  ``IW64``  ``ID0``   (German ``E`` also accepted)
* Outputs:      ``Q0.1``  ``QB0``  ``QW64``  ``QD0``   (German ``A`` also accepted)

Size letter rules: ``X``/bit form -> BOOL; ``W`` -> 2-byte types; ``D`` -> 4-byte types;
``B`` -> any type (start byte), required for 8-byte types, STRING/WSTRING and arrays of
multi-byte types. Absolute addressing works only with NON-optimized DBs.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any

from drivers.codecs.datatypes import DATA_TYPES, DataType, ValueKind
from utils.exceptions import AddressError, EncodingError

S7_SUPPORTED_TYPES = frozenset(DataType)

_DB_RE = re.compile(r"^DB(\d{1,5})\.DB([XBWD])(\d{1,5})(?:\.([0-7]))?$", re.I)
_AREA_RE = re.compile(r"^([MIEQA])([BWD]?)(\d{1,5})(?:\.([0-7]))?$", re.I)
_AREA_MAP = {"M": "MK", "I": "PE", "E": "PE", "Q": "PA", "A": "PA"}
_LETTER_SIZE = {"W": 2, "D": 4}


@dataclass(frozen=True, slots=True)
class S7Address:
    area: str            # "DB" | "MK" | "PE" | "PA"
    db_number: int       # 0 for non-DB areas
    start: int           # byte offset
    bit: int | None      # bit index for BOOL
    size: int            # total bytes covered (element size * count)
    data_type: DataType
    count: int           # array length (1 for scalar)
    string_length: int | None

    @property
    def end(self) -> int:
        return self.start + self.size


def element_size(data_type: DataType, string_length: int | None) -> int:
    if data_type is DataType.STRING:
        return 2 + (string_length or 254)
    if data_type is DataType.WSTRING:
        return 4 + 2 * (string_length or 254)
    size = DATA_TYPES[data_type].size
    assert size is not None
    return size


def parse_s7_address(
    address: str,
    data_type: DataType,
    *,
    array_length: int | None = None,
    string_length: int | None = None,
) -> S7Address:
    text = address.strip().replace(" ", "")
    count = array_length or 1
    m = _DB_RE.match(text)
    if m:
        area, db_number = "DB", int(m.group(1))
        letter, start, bit_s = m.group(2).upper(), int(m.group(3)), m.group(4)
        if db_number < 1:
            raise AddressError(f"{address}: DB number must be >= 1")
    else:
        m = _AREA_RE.match(text)
        if not m:
            raise AddressError(f"{address}: not a valid S7 absolute address")
        area, db_number = _AREA_MAP[m.group(1).upper()], 0
        letter = m.group(2).upper() or ("X" if m.group(4) is not None else "")
        start, bit_s = int(m.group(3)), m.group(4)
        if letter == "":
            raise AddressError(f"{address}: missing size letter or bit (e.g. MW10, M5.1)")

    bit: int | None = None
    if letter == "X":
        if bit_s is None:
            raise AddressError(f"{address}: bit address requires .bit (0-7)")
        if data_type is not DataType.BOOL or count != 1:
            raise AddressError(f"{address}: bit address requires scalar BOOL")
        bit = int(bit_s)
    elif bit_s is not None:
        raise AddressError(f"{address}: bit index only valid with X / bit form")
    elif data_type is DataType.BOOL:
        raise AddressError(f"{address}: BOOL requires a bit address such as DB1.DBX0.0")

    esize = element_size(data_type, string_length)
    if letter in _LETTER_SIZE:
        if esize != _LETTER_SIZE[letter]:
            raise AddressError(f"{address}: size letter {letter} does not match {data_type} ({esize} bytes)")
    if data_type in (DataType.STRING, DataType.WSTRING) and count != 1:
        raise AddressError(f"{address}: arrays of strings are not supported")
    size = esize * count if bit is None else 1
    return S7Address(area, db_number, start, bit, size, data_type, count, string_length)


def _decode_scalar(data: bytes, offset: int, data_type: DataType, string_length: int | None) -> Any:
    info = DATA_TYPES[data_type]
    if data_type is DataType.STRING:
        max_len = data[offset]
        cur_len = data[offset + 1]
        declared = string_length or 254
        if cur_len > max_len or cur_len > declared:
            raise EncodingError(f"STRING header invalid (max={max_len}, len={cur_len})")
        return bytes(data[offset + 2: offset + 2 + cur_len]).decode("latin-1")
    if data_type is DataType.WSTRING:
        max_len, cur_len = struct.unpack_from(">HH", data, offset)
        if cur_len > max_len:
            raise EncodingError("WSTRING header invalid")
        return bytes(data[offset + 4: offset + 4 + 2 * cur_len]).decode("utf-16-be")
    if data_type is DataType.CHAR:
        return bytes(data[offset: offset + 1]).decode("latin-1")
    assert info.struct_code is not None
    (value,) = struct.unpack_from(">" + info.struct_code, data, offset)
    return value


def decode_s7(data: bytes | bytearray, addr: S7Address, base: int = 0) -> Any:
    """Decode value for ``addr`` from ``data`` whose first byte is at absolute offset ``base``."""
    offset = addr.start - base
    if offset < 0 or offset + addr.size > len(data):
        raise EncodingError("buffer does not cover address range")
    if addr.bit is not None:
        return bool((data[offset] >> addr.bit) & 1)
    if addr.count == 1:
        return _decode_scalar(data, offset, addr.data_type, addr.string_length)
    esize = element_size(addr.data_type, addr.string_length)
    return [_decode_scalar(data, offset + i * esize, addr.data_type, addr.string_length)
            for i in range(addr.count)]


def _encode_scalar(value: Any, data_type: DataType, string_length: int | None) -> bytes:
    info = DATA_TYPES[data_type]
    if data_type is DataType.STRING:
        max_len = string_length or 254
        raw = str(value).encode("latin-1")
        if len(raw) > max_len:
            raise EncodingError("string exceeds declared length")
        return bytes((max_len, len(raw))) + raw.ljust(max_len, b"\x00")
    if data_type is DataType.WSTRING:
        max_len = string_length or 254
        raw = str(value).encode("utf-16-be")
        if len(raw) // 2 > max_len:
            raise EncodingError("wstring exceeds declared length")
        return struct.pack(">HH", max_len, len(raw) // 2) + raw.ljust(max_len * 2, b"\x00")
    if data_type is DataType.CHAR:
        raw = str(value).encode("latin-1")
        if len(raw) != 1:
            raise EncodingError("CHAR requires exactly one character")
        return raw
    assert info.struct_code is not None
    if info.kind is ValueKind.INT and (isinstance(value, bool) or not isinstance(value, int)):
        raise EncodingError(f"{data_type} requires int")
    try:
        return struct.pack(">" + info.struct_code, value)
    except (struct.error, OverflowError) as exc:
        raise EncodingError(f"value {value!r} out of range for {data_type}") from exc


def encode_s7(value: Any, addr: S7Address) -> bytes:
    """Encode a non-BOOL value for a byte-oriented write. BOOL writes use bit access."""
    if addr.bit is not None:
        raise EncodingError("BOOL values are written with bit access, not byte encoding")
    if addr.count == 1:
        return _encode_scalar(value, addr.data_type, addr.string_length)
    if not isinstance(value, list) or len(value) != addr.count:
        raise EncodingError(f"expected list of {addr.count} elements")
    return b"".join(_encode_scalar(v, addr.data_type, addr.string_length) for v in value)
