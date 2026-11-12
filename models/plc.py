"""Device (PLC) configuration and runtime info models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from models.tag import ID_PATTERN, StrictModel


class DriverName(enum.StrEnum):
    SIEMENS_S7 = "siemens_s7"
    ROCKWELL_LOGIX = "rockwell_logix"
    MITSUBISHI_MC = "mitsubishi_mc"
    MODBUS_TCP = "modbus_tcp"
    MODBUS_RTU = "modbus_rtu"
    OPC_UA = "opc_ua"
    OPC_DA = "opc_da"


class RetryPolicy(StrictModel):
    initial_delay_s: float = Field(default=1.0, gt=0, le=600)
    max_delay_s: float = Field(default=60.0, gt=0, le=3600)
    jitter: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> RetryPolicy:
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("max_delay_s must be >= initial_delay_s")
        return self


class CircuitBreakerConfig(StrictModel):
    failure_threshold: int = Field(default=5, ge=1, le=1000)
    open_duration_s: float = Field(default=30.0, gt=0, le=3600)


ByteOrder = Literal["big", "little"]


class S7Connection(StrictModel):
    driver: Literal["siemens_s7"] = "siemens_s7"
    host: str
    port: int = Field(default=102, ge=1, le=65535)
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    timeout_s: float = Field(default=5.0, gt=0, le=60)
    connection_type: Literal["PG", "OP", "S7_BASIC"] = "PG"
    max_gap_bytes: int = Field(default=32, ge=0, le=512)
    max_block_bytes: int = Field(default=400, ge=16, le=8192)


class LogixConnection(StrictModel):
    driver: Literal["rockwell_logix"] = "rockwell_logix"
    path: str = Field(min_length=7)
    timeout_s: float = Field(default=5.0, gt=0, le=60)
    init_tags: bool = True
    program_tags: bool = True


class MCConnection(StrictModel):
    driver: Literal["mitsubishi_mc"] = "mitsubishi_mc"
    host: str
    port: int = Field(ge=1, le=65535)
    frame: Literal["3E", "1E"] = "3E"
    plc_series: Literal["FX3U", "FX5U", "Q", "L", "iQ-R"]
    network_no: int = Field(default=0, ge=0, le=255)
    pc_no: int = Field(default=0xFF, ge=0, le=255)
    dest_module_io: int = Field(default=0x03FF, ge=0, le=0xFFFF)
    dest_module_station: int = Field(default=0, ge=0, le=255)
    timeout_s: float = Field(default=3.0, gt=0, le=60)
    monitoring_timer_ms: int = Field(default=2000, ge=250, le=60000)
    max_word_points: int | None = Field(default=None, ge=1, le=960)
    max_bit_points: int | None = Field(default=None, ge=1, le=7168)
    max_gap: int = Field(default=8, ge=0, le=256)

    @model_validator(mode="after")
    def _check(self) -> MCConnection:
        if self.plc_series == "FX3U" and self.frame != "1E":
            raise ValueError("FX3U Ethernet adapters (FX3U-ENET-ADP / ENET-L) use the 1E frame")
        if self.frame == "1E" and self.plc_series != "FX3U":
            raise ValueError("1E frame is only configured for FX3U in this gateway")
        return self

    @property
    def word_limit(self) -> int:
        return self.max_word_points or (64 if self.frame == "1E" else 960)

    @property
    def bit_limit(self) -> int:
        return self.max_bit_points or (256 if self.frame == "1E" else 3584)


class ModbusTcpConnection(StrictModel):
    driver: Literal["modbus_tcp"] = "modbus_tcp"
    host: str
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=255)
    timeout_s: float = Field(default=3.0, gt=0, le=60)
    byte_order: ByteOrder = "big"
    word_order: ByteOrder = "big"
    max_registers_per_read: int = Field(default=125, ge=1, le=125)
    max_bits_per_read: int = Field(default=2000, ge=1, le=2000)
    max_gap: int = Field(default=10, ge=0, le=100)


class ModbusRtuConnection(StrictModel):
    driver: Literal["modbus_rtu"] = "modbus_rtu"
    serial_port: str = Field(min_length=1)
    baudrate: int = Field(default=9600, ge=300, le=921600)
    bytesize: Literal[7, 8] = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    unit_id: int = Field(default=1, ge=1, le=247)
    timeout_s: float = Field(default=1.0, gt=0, le=30)
    byte_order: ByteOrder = "big"
    word_order: ByteOrder = "big"
    max_registers_per_read: int = Field(default=125, ge=1, le=125)
    max_bits_per_read: int = Field(default=2000, ge=1, le=2000)
    max_gap: int = Field(default=10, ge=0, le=100)


class OpcUaConnection(StrictModel):
    driver: Literal["opc_ua"] = "opc_ua"
    endpoint: str = Field(pattern=r"^opc\.tcp://")
    security_policy: Literal["None", "Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss"] = (
        "Basic256Sha256")
    security_mode: Literal["None", "Sign", "SignAndEncrypt"] = "SignAndEncrypt"
    client_cert: str | None = None
    client_key: str | None = None
    client_key_password_env: str | None = None
    server_cert: str | None = None
    application_uri: str = "urn:industrial-plc-gateway:client"
    username_env: str | None = None
    password_env: str | None = None
    allow_insecure: bool = False
    timeout_s: float = Field(default=5.0, gt=0, le=60)
    max_nodes_per_read: int = Field(default=500, ge=1, le=10000)

    @model_validator(mode="after")
    def _check(self) -> OpcUaConnection:
        if self.security_policy == "None" or self.security_mode == "None":
            if not self.allow_insecure:
                raise ValueError("OPC UA without security requires allow_insecure: true (not recommended)")
            if self.security_policy != "None" or self.security_mode != "None":
                raise ValueError("security_policy and security_mode must both be None for insecure mode")
        else:
            if not (self.client_cert and self.client_key):
                raise ValueError("secure OPC UA requires client_cert and client_key")
            if not self.server_cert and not self.allow_insecure:
                raise ValueError("server_cert is required to verify the server (set allow_insecure to skip)")
        if bool(self.username_env) != bool(self.password_env):
            raise ValueError("username_env and password_env must be set together")
        return self


class OpcDaConnection(StrictModel):
    driver: Literal["opc_da"] = "opc_da"
    server_progid: str = Field(min_length=1)
    host: str = "localhost"
    automation_progid: str = "OPC.Automation"
    bridge_python: str | None = None   # path to (often 32-bit) python.exe with pywin32
    timeout_s: float = Field(default=10.0, gt=0, le=120)


ConnectionConfig = Annotated[
    S7Connection | LogixConnection | MCConnection | ModbusTcpConnection | ModbusRtuConnection
    | OpcUaConnection | OpcDaConnection,
    Field(discriminator="driver"),
]


class DeviceConfig(StrictModel):
    device_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    driver: DriverName
    enabled: bool = True
    write_enabled: bool = False
    tags_file: str = "tags.yaml"
    connection: ConnectionConfig
    reconnect: RetryPolicy = Field(default_factory=RetryPolicy)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    plant_id: str = Field(default="", exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _inject_driver(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("connection"), dict) and "driver" in data:
            conn = dict(data["connection"])
            if "driver" in conn and conn["driver"] != data["driver"]:
                raise ValueError("connection.driver must match device driver")
            conn["driver"] = str(data["driver"])
            data = {**data, "connection": conn}
        return data


class PlantConfig(StrictModel):
    plant_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    devices: list[DeviceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _assign(self) -> PlantConfig:
        for device in self.devices:
            device.plant_id = self.plant_id
        return self


class PlantsFile(StrictModel):
    plants: list[PlantConfig]


# ---------------------------------------------------------------- runtime records


class DeviceStatus(enum.StrEnum):
    DISABLED = "DISABLED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    STOPPED = "STOPPED"


@dataclass(slots=True)
class DeviceInfo:
    device_id: str
    driver: str
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DriverHealth:
    device_id: str
    connected: bool
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None
    checked_at: datetime | None = None
