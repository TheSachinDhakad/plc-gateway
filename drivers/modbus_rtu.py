"""Modbus RTU driver over RS485/RS232 (pymodbus 3.16 ``AsyncModbusSerialClient`` + pyserial).

One device entry represents one serial bus; several slaves on the bus are addressed with
``unit@table:offset`` tag addresses so requests are naturally serialized on the bus.
Broadcast (unit 0) is not used for reads.
"""

from __future__ import annotations

from typing import Any

from pymodbus import FramerType
from pymodbus.client import AsyncModbusSerialClient

from drivers.modbus_common import ModbusDriverBase
from drivers.serial import serial_port_exists
from models.plc import DeviceConfig, ModbusRtuConnection
from utils.exceptions import DriverConnectionError


class ModbusRtuDriver(ModbusDriverBase):
    driver_name = "modbus_rtu"

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, ModbusRtuConnection)
        self.rtu: ModbusRtuConnection = device.connection

    def _make_client(self) -> AsyncModbusSerialClient:
        return AsyncModbusSerialClient(
            self.rtu.serial_port,
            framer=FramerType.RTU,
            baudrate=self.rtu.baudrate,
            bytesize=self.rtu.bytesize,
            parity=self.rtu.parity,
            stopbits=self.rtu.stopbits,
            timeout=self.rtu.timeout_s,
            retries=0,
            reconnect_delay=0,
            reconnect_delay_max=0,
            name=f"rtu_{self.device_id}",
        )

    async def connect(self) -> None:
        if not serial_port_exists(self.rtu.serial_port):
            raise DriverConnectionError(f"{self.device_id}: serial port {self.rtu.serial_port} not found")
        await super().connect()

    def _endpoint(self) -> str:
        r = self.rtu
        return f"{r.serial_port} {r.baudrate} {r.bytesize}{r.parity}{r.stopbits}"
