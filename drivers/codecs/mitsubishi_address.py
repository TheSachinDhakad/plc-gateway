"""Mitsubishi device address parser (MC protocol / SLMP).

Numbering bases differ by series and device — the most common source of wrong addresses:

* FX3U / FX5U: ``X`` and ``Y`` are OCTAL (``X17`` is the 16th input, number 15).
* Q / L / iQ-R: ``X``, ``Y``, ``B``, ``W``, ``SB``, ``SW`` are HEXADECIMAL.
* FX5U: ``B``/``W``/``SB``/``SW`` are hexadecimal as on Q.
* All other devices are decimal.

Bit access inside a word device is written ``D100.3`` (bit 3 of D100), read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.exceptions import AddressError

Series = str  # "FX3U" | "FX5U" | "Q" | "L" | "iQ-R"

# device -> (is_bit_device, 3E binary device code, 1E binary device code or None)
DEVICES: dict[str, tuple[bool, int, int | None]] = {
    "X": (True, 0x9C, 0x5820),
    "Y": (True, 0x9D, 0x5920),
    "M": (True, 0x90, 0x4D20),
    "L": (True, 0x92, None),
    "F": (True, 0x93, None),
    "V": (True, 0x94, None),
    "B": (True, 0xA0, None),
    "S": (True, 0x98, 0x5320),
    "SM": (True, 0x91, None),
    "SB": (True, 0xA1, None),
    "TS": (True, 0xC1, 0x5453),
    "TC": (True, 0xC0, None),
    "CS": (True, 0xC4, 0x4353),
    "CC": (True, 0xC3, None),
    "D": (False, 0xA8, 0x4420),
    "W": (False, 0xB4, None),
    "R": (False, 0xAF, 0x5220),
    "ZR": (False, 0xB0, None),
    "SD": (False, 0xA9, None),
    "SW": (False, 0xB5, None),
    "Z": (False, 0xCC, None),
    "TN": (False, 0xC2, 0x544E),
    "CN": (False, 0xC5, 0x434E),
}

_SERIES_DEVICES: dict[str, frozenset[str]] = {
    "FX3U": frozenset({"X", "Y", "M", "S", "TS", "CS", "D", "R", "TN", "CN"}),
    "FX5U": frozenset({"X", "Y", "M", "L", "F", "B", "S", "SM", "SB", "TS", "TC", "CS", "CC",
                       "D", "W", "R", "SD", "SW", "Z", "TN", "CN"}),
    "Q": frozenset(DEVICES) - {"S"},
    "L": frozenset(DEVICES) - {"S"},
    "iQ-R": frozenset(DEVICES) - {"S"},
}

_HEX_DEVICES_Q = frozenset({"X", "Y", "B", "W", "SB", "SW"})
_HEX_DEVICES_FX5 = frozenset({"B", "W", "SB", "SW"})
_OCTAL_DEVICES_FX = frozenset({"X", "Y"})

_ADDR_RE = re.compile(r"^(ZR|SM|SD|SB|SW|TS|TC|TN|CS|CC|CN|[XYMLFVBSDWRZ])([0-9A-F]+)(?:\.([0-9A-F]))?$", re.I)


@dataclass(frozen=True, slots=True)
class MCAddress:
    device: str
    number: int
    is_bit_device: bool
    bit: int | None = None     # bit-in-word for word devices

    def code_3e(self) -> int:
        return DEVICES[self.device][1]

    def code_1e(self) -> int:
        code = DEVICES[self.device][2]
        if code is None:
            raise AddressError(f"device {self.device} not available in MC 1E frame")
        return code


def number_base(device: str, series: Series) -> int:
    if series in ("FX3U", "FX5U") and device in _OCTAL_DEVICES_FX:
        return 8
    if series == "FX5U" and device in _HEX_DEVICES_FX5:
        return 16
    if series in ("Q", "L", "iQ-R") and device in _HEX_DEVICES_Q:
        return 16
    return 10


def parse_mc_address(address: str, series: Series) -> MCAddress:
    if series not in _SERIES_DEVICES:
        raise AddressError(f"unknown PLC series {series!r}")
    m = _ADDR_RE.match(address.strip().upper())
    if not m:
        raise AddressError(f"{address}: not a valid Mitsubishi device address")
    device, num_s, bit_s = m.group(1), m.group(2), m.group(3)
    if device not in _SERIES_DEVICES[series]:
        raise AddressError(f"{address}: device {device} not supported on {series}")
    base = number_base(device, series)
    try:
        number = int(num_s, base)
    except ValueError as exc:
        raise AddressError(f"{address}: {num_s} is not a valid base-{base} number for {device}") from exc
    is_bit = DEVICES[device][0]
    bit = None
    if bit_s is not None:
        if is_bit:
            raise AddressError(f"{address}: bit suffix only allowed on word devices")
        bit = int(bit_s, 16)
    if number > 0xFFFFFF and series != "iQ-R":
        raise AddressError(f"{address}: device number out of range")
    return MCAddress(device, number, is_bit, bit)


def format_mc_address(addr: MCAddress, series: Series) -> str:
    base = number_base(addr.device, series)
    num = {8: f"{addr.number:o}", 16: f"{addr.number:X}", 10: str(addr.number)}[base]
    suffix = f".{addr.bit:X}" if addr.bit is not None else ""
    return f"{addr.device}{num}{suffix}"
