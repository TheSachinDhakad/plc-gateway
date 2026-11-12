"""Driver registry. Modules are imported lazily so platform-specific dependencies
(pywin32 for OPC DA) are only imported when a device actually uses them."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from utils.exceptions import ConfigError

if TYPE_CHECKING:
    from drivers.base import BaseDriver

DRIVER_REGISTRY: dict[str, str] = {
    "siemens_s7": "drivers.siemens_s7:SiemensS7Driver",
    "rockwell_logix": "drivers.rockwell:RockwellLogixDriver",
    "mitsubishi_mc": "drivers.mitsubishi:MitsubishiMCDriver",
    "modbus_tcp": "drivers.modbus_tcp:ModbusTcpDriver",
    "modbus_rtu": "drivers.modbus_rtu:ModbusRtuDriver",
    "opc_ua": "drivers.opc_ua:OpcUaDriver",
    "opc_da": "drivers.opc_da:OpcDaDriver",
}


def get_driver_class(name: str) -> type[BaseDriver]:
    try:
        target = DRIVER_REGISTRY[str(name)]
    except KeyError as exc:
        raise ConfigError(f"unknown driver {name!r}") from exc
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)
