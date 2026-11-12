"""Siemens address/codec and Modbus register codec tests (pure, no I/O)."""

from __future__ import annotations

import math
import struct

import pytest

from drivers.codecs.datatypes import DataType
from drivers.codecs.register_codec import decode_registers, encode_registers, register_count
from drivers.codecs.siemens_address import decode_s7, encode_s7, parse_s7_address
from utils.exceptions import AddressError, EncodingError

# ---------------------------------------------------------------------------------------- Siemens


@pytest.mark.parametrize("address,dtype,area,db,start,bit,size", [
    ("DB100.DBD0", "REAL", "DB", 100, 0, None, 4),
    ("DB100.DBW8", "INT", "DB", 100, 8, None, 2),
    ("DB100.DBX10.0", "BOOL", "DB", 100, 10, 0, 1),
    ("db1.dbb2", "BYTE", "DB", 1, 2, None, 1),
    ("DB5.DBB0", "LREAL", "DB", 5, 0, None, 8),
    ("MW10", "WORD", "MK", 0, 10, None, 2),
    ("M5.1", "BOOL", "MK", 0, 5, 1, 1),
    ("MD20", "DINT", "MK", 0, 20, None, 4),
    ("I0.0", "BOOL", "PE", 0, 0, 0, 1),
    ("E0.7", "BOOL", "PE", 0, 0, 7, 1),
    ("QW64", "INT", "PA", 0, 64, None, 2),
    ("A1.2", "BOOL", "PA", 0, 1, 2, 1),
])
def test_s7_parse_valid(address, dtype, area, db, start, bit, size):
    a = parse_s7_address(address, DataType(dtype))
    assert (a.area, a.db_number, a.start, a.bit, a.size) == (area, db, start, bit, size)


@pytest.mark.parametrize("address,dtype", [
    ("DB100.DBD0", "INT"),        # size letter mismatch
    ("DB100.DBW0", "REAL"),
    ("DB100.DBX10", "BOOL"),      # missing bit
    ("DB100.DBX10.8", "BOOL"),    # bit out of range
    ("DB100.DBD0", "BOOL"),       # BOOL without bit
    ("DB0.DBW0", "INT"),          # DB 0 invalid
    ("DB100.DBW0.1", "INT"),      # bit on word
    ("XYZ", "INT"),
    ("M10", "INT"),               # missing size letter
])
def test_s7_parse_invalid(address, dtype):
    with pytest.raises(AddressError):
        parse_s7_address(address, DataType(dtype))


def test_s7_decode_types_big_endian():
    data = bytearray(64)
    struct.pack_into(">f", data, 0, 25.5)
    struct.pack_into(">h", data, 4, -1234)
    struct.pack_into(">i", data, 6, 123456789)
    struct.pack_into(">d", data, 10, math.pi)
    data[18] = 0b0000_0101
    assert decode_s7(data, parse_s7_address("DB1.DBD0", DataType.REAL)) == pytest.approx(25.5)
    assert decode_s7(data, parse_s7_address("DB1.DBW4", DataType.INT)) == -1234
    assert decode_s7(data, parse_s7_address("DB1.DBD6", DataType.DINT)) == 123456789
    assert decode_s7(data, parse_s7_address("DB1.DBB10", DataType.LREAL)) == pytest.approx(math.pi)
    assert decode_s7(data, parse_s7_address("DB1.DBX18.0", DataType.BOOL)) is True
    assert decode_s7(data, parse_s7_address("DB1.DBX18.1", DataType.BOOL)) is False
    assert decode_s7(data, parse_s7_address("DB1.DBX18.2", DataType.BOOL)) is True


def test_s7_decode_with_block_base_offset():
    block = struct.pack(">f", 1.5) + struct.pack(">h", 7)
    addr = parse_s7_address("DB1.DBW104", DataType.INT)
    assert decode_s7(block, addr, base=100) == 7


def test_s7_string_roundtrip_and_header_validation():
    addr = parse_s7_address("DB1.DBB20", DataType.STRING, string_length=10)
    assert addr.size == 12
    raw = encode_s7("HELLO", addr)
    assert raw[:2] == bytes((10, 5))
    assert decode_s7(raw, addr, base=20) == "HELLO"
    bad = bytearray(raw)
    bad[1] = 50   # actual length > max length
    with pytest.raises(EncodingError):
        decode_s7(bad, addr, base=20)


def test_s7_wstring_and_arrays():
    w = parse_s7_address("DB1.DBB0", DataType.WSTRING, string_length=4)
    assert decode_s7(encode_s7("Äb€", w), w) == "Äb€"
    arr = parse_s7_address("DB1.DBB0", DataType.REAL, array_length=3)
    assert arr.size == 12
    assert decode_s7(encode_s7([1.0, 2.0, 3.5], arr), arr) == [1.0, 2.0, 3.5]


def test_s7_encode_rejects_bool_and_range():
    with pytest.raises(EncodingError):
        encode_s7(True, parse_s7_address("DB1.DBX0.0", DataType.BOOL))
    with pytest.raises(EncodingError):
        encode_s7(40000, parse_s7_address("DB1.DBW0", DataType.INT))


# ---------------------------------------------------------------------------------------- registers

@pytest.mark.parametrize("byte_order,word_order,expected", [
    ("big", "big", [0x1122, 0x3344]),
    ("little", "big", [0x2211, 0x4433]),
    ("big", "little", [0x3344, 0x1122]),
    ("little", "little", [0x4433, 0x2211]),
])
def test_register_order_permutations(byte_order, word_order, expected):
    regs = encode_registers(0x11223344, DataType.UDINT, byte_order=byte_order, word_order=word_order)
    assert regs == expected
    assert decode_registers(regs, DataType.UDINT, byte_order=byte_order, word_order=word_order) == 0x11223344


@pytest.mark.parametrize("dtype,value", [
    ("INT", -2), ("UINT", 65535), ("DINT", -100000), ("UDINT", 4_000_000_000), ("REAL", 230.25),
    ("LREAL", -1.0e300), ("LINT", -(2**62)), ("ULINT", 2**64 - 1),
])
@pytest.mark.parametrize("word_order", ["big", "little"])
def test_register_roundtrip(dtype, value, word_order):
    dt = DataType(dtype)
    regs = encode_registers(value, dt, word_order=word_order)
    assert len(regs) == register_count(dt)
    assert decode_registers(regs, dt, word_order=word_order) == pytest.approx(value)


def test_register_known_float_cdab():
    # 230.0f = 0x43660000 ; CDAB order puts low word first
    assert decode_registers([0x0000, 0x4366], DataType.REAL, word_order="little") == 230.0


def test_register_string():
    regs = encode_registers("ABC", DataType.STRING, string_length=4)
    assert regs == [0x4142, 0x4300]
    assert decode_registers(regs, DataType.STRING, string_length=4) == "ABC"
    assert decode_registers([0x4241, 0x0043], DataType.STRING, string_length=4, byte_order="little") == "ABC"


def test_register_errors():
    with pytest.raises(EncodingError):
        decode_registers([1], DataType.DINT)
    with pytest.raises(EncodingError):
        encode_registers(70000, DataType.INT)
    with pytest.raises(EncodingError):
        encode_registers(float("nan"), DataType.REAL)
    with pytest.raises(EncodingError):
        register_count(DataType.BOOL)
