"""Mitsubishi MC framing and address parsing (pure)."""

from __future__ import annotations

import pytest

from drivers import mc_protocol as mc
from drivers.codecs.mitsubishi_address import format_mc_address, parse_mc_address
from drivers.mitsubishi import decode_words, encode_words, plan_tag
from tests.conftest import make_tag
from utils.exceptions import AddressError, ProtocolError, TagConfigError


def test_3e_batch_read_words_frame_matches_spec_example():
    # Read D100..D102 (3 points), network 0, PC 0xFF, IO 0x03FF, station 0, timer 0x0010
    frame = mc.build_3e_batch_read(0xA8, 100, 3, bit_units=False, iqr=False, route=mc.Route3E(), timer_units=0x10)
    assert frame.hex() == "500000ffff03000c00100001040000640000a80300"


def test_3e_batch_write_bits_frame():
    frame = mc.build_3e_batch_write(0x90, 10, bits=[True, False, True], iqr=False, route=mc.Route3E(),
                                    timer_units=4)
    # ... cmd 1401 sub 0001, dev M10 (0a0000 90), points 3, data 10 10
    assert frame.endswith(bytes.fromhex("011401000a000090030010" + "10"))


def test_3e_iqr_device_spec_is_4_plus_2_bytes():
    frame = mc.build_3e_batch_read(0xA8, 70000, 1, bit_units=False, iqr=True, route=mc.Route3E(), timer_units=4)
    assert frame[13:15] == bytes.fromhex("0200")          # subcommand 0x0002
    assert frame[15:19] == (70000).to_bytes(4, "little")
    assert frame[19:21] == bytes.fromhex("a800")


def test_3e_response_parsing_and_end_code():
    header = bytes.fromhex("d00000ffff0300") + (6).to_bytes(2, "little")
    assert mc.parse_3e_response_header(header) == 6
    assert mc.parse_3e_response_body(bytes.fromhex("0000") + bytes.fromhex("01000200")) == bytes.fromhex("01000200")
    with pytest.raises(mc.MCEndCodeError) as err:
        mc.parse_3e_response_body(bytes.fromhex("56c0"))
    assert err.value.end_code == 0xC056
    with pytest.raises(ProtocolError):
        mc.parse_3e_response_header(bytes(9))


def test_1e_frames():
    frame = mc.build_1e_batch_read(0x4420, 100, 2, bit_units=False, pc_no=0xFF, timer_units=10)
    assert frame.hex() == "01ff0a0064000000204402" + "00"
    w = mc.build_1e_batch_write(0x4D20, 0, bits=[True, True, False], pc_no=0xFF, timer_units=10)
    assert w[0] == mc.CMD1E_WRITE_BIT and w[-2:] == bytes((0x11, 0x00))
    assert mc.expected_1e_data_len(mc.CMD1E_READ_BIT, 3) == 2
    assert mc.check_1e_response_head(bytes((0x81, 0x00)), mc.CMD1E_READ_WORD) == 0
    with pytest.raises(ProtocolError):
        mc.check_1e_response_head(bytes((0x80, 0x00)), mc.CMD1E_READ_WORD)


def test_bit_packing_roundtrip():
    values = [True, False, False, True, True]
    assert mc.unpack_bits(mc.pack_bits(values), 5) == values


@pytest.mark.parametrize("address,series,device,number", [
    ("X17", "FX5U", "X", 15),       # octal on FX
    ("Y10", "FX3U", "Y", 8),
    ("X1F", "Q", "X", 31),          # hex on Q
    ("W1A", "Q", "W", 26),
    ("W1A", "FX5U", "W", 26),
    ("D100", "FX3U", "D", 100),
    ("ZR1000", "iQ-R", "ZR", 1000),
    ("SM400", "Q", "SM", 400),
])
def test_mc_address_bases(address, series, device, number):
    a = parse_mc_address(address, series)
    assert (a.device, a.number) == (device, number)
    assert format_mc_address(a, series) == address


@pytest.mark.parametrize("address,series", [("X18", "FX5U"), ("W10", "FX3U"), ("ZZ1", "Q"), ("M10.1", "Q")])
def test_mc_address_invalid(address, series):
    with pytest.raises(AddressError):
        parse_mc_address(address, series)


def test_mc_word_codec_little_endian():
    tag = make_tag("t", "D200", "DINT")
    words = encode_words(-123456, tag)
    assert words == [0x1DC0, 0xFFFE]          # low word first
    assert decode_words(words, tag, None) == -123456
    real = make_tag("r", "D10", "REAL")
    assert decode_words(encode_words(1.5, real), real, None) == 1.5
    s = make_tag("s", "D20", "STRING", string_length=5)
    assert decode_words(encode_words("ABCDE", s), s, None) == "ABCDE"
    b = make_tag("b", "D1.3", "BOOL")
    assert decode_words([0b1000], b, 3) is True


def test_mc_plan_validation():
    assert plan_tag(make_tag("a", "D100", "REAL", array_length=4), "FX5U").points == 8
    with pytest.raises(TagConfigError):
        plan_tag(make_tag("a", "M10", "INT"), "FX5U")
    with pytest.raises(TagConfigError):
        plan_tag(make_tag("a", "D10", "BOOL"), "FX5U")
