"""Modbus TCP driver (pymodbus 3.16 ``AsyncModbusTcpClient``).

pymodbus' own auto-reconnect is disabled (``reconnect_delay=0``) and its retries set to 0:
the gateway ConnectionManager owns reconnect/backoff/circuit breaking.
"""

from __future__ import annotations

from typing import Any

from pymodbus.client import AsyncModbusTcpClient

from drivers.modbus_common import ModbusDriverBase
from models.plc import DeviceConfig, ModbusTcpConnection


class ModbusTcpDriver(ModbusDriverBase):
    driver_name = "modbus_tcp"

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, ModbusTcpConnection)
        self.tcp: ModbusTcpConnection = device.connection

    def _make_client(self) -> AsyncModbusTcpClient:
        return AsyncModbusTcpClient(
            self.tcp.host,
            port=self.tcp.port,
            timeout=self.tcp.timeout_s,
            retries=0,
            reconnect_delay=0,
            reconnect_delay_max=0,
            name=f"mb_{self.device_id}",
        )

    def _endpoint(self) -> str:
        return f"{self.tcp.host}:{self.tcp.port}"
