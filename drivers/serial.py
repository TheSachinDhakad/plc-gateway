"""Serial (RS232/RS485) physical-layer helpers.

RS232/RS485 are physical interfaces, not protocols. Supported serial protocol in this
gateway: Modbus RTU (``drivers.modbus_rtu``). Vendor serial protocols (e.g. Mitsubishi
computer link) are not implemented and are rejected at configuration time.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from serial.tools import list_ports


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str


def list_serial_ports() -> list[SerialPortInfo]:
    return [SerialPortInfo(p.device, p.description or "", p.hwid or "") for p in list_ports.comports()]


def serial_port_exists(port: str) -> bool:
    """Best-effort existence check (Windows COM ports are enumerated; POSIX paths are stat'ed)."""
    if sys.platform == "win32":
        wanted = port.upper().removeprefix("\\\\.\\")
        return any(p.device.upper() == wanted for p in list_ports.comports())
    return os.path.exists(port)


def character_time_s(baudrate: int, bytesize: int = 8, parity: str = "N", stopbits: int = 1) -> float:
    """Time to transmit one character (start + data + parity + stop bits)."""
    bits = 1 + bytesize + (0 if parity == "N" else 1) + stopbits
    return bits / baudrate


def rtu_inter_frame_delay_s(baudrate: int, bytesize: int = 8, parity: str = "N", stopbits: int = 1) -> float:
    """Modbus RTU t3.5: 3.5 character times, fixed at 1.75 ms above 19200 baud (spec)."""
    if baudrate > 19200:
        return 0.00175
    return 3.5 * character_time_s(baudrate, bytesize, parity, stopbits)
