"""Runtime settings (settings.yaml) and the fully validated GatewayConfig aggregate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from models.plc import DeviceConfig, PlantConfig, RetryPolicy
from models.tag import ID_PATTERN, StrictModel, TagConfig


class GatewaySection(StrictModel):
    gateway_id: str = Field(pattern=ID_PATTERN)
    environment: Literal["development", "production"] = "production"
    status_file: str = "data/status.json"
    read_only_mode: bool = True
    write_kill_switch_file: str = "data/WRITE_DISABLE"
    data_dir: str = "data"
    log_dir: str = "logs"


class BufferSection(StrictModel):
    queue_max_size: int = Field(default=50_000, ge=100, le=5_000_000)
    durability_mode: Literal["spool_first", "memory_first"] = "spool_first"
    spool_path: str = "data/spool.sqlite3"
    spool_max_rows: int = Field(default=20_000_000, ge=1000)
    overflow_policy: Literal["drop_cyclic_keep_events", "drop_oldest", "reject_new"] = (
        "drop_cyclic_keep_events")
    flush_interval_ms: int = Field(default=1000, ge=50, le=60_000)
    batch_size: int = Field(default=1000, ge=1, le=20_000)


class DatabaseSection(StrictModel):
    enabled: bool = True
    host_env: str = "MYSQL_HOST"
    port_env: str = "MYSQL_PORT"
    database_env: str = "MYSQL_DATABASE"
    user_env: str = "MYSQL_USER"
    password_env: str = "MYSQL_PASSWORD"  # noqa: S105 - env var name
    ssl_ca_env: str = "MYSQL_SSL_CA"
    maint_user_env: str = "MYSQL_MAINT_USER"
    maint_password_env: str = "MYSQL_MAINT_PASSWORD"  # noqa: S105 - env var name
    pool_min: int = Field(default=1, ge=1, le=50)
    pool_max: int = Field(default=5, ge=1, le=100)
    connect_timeout_s: float = Field(default=10, gt=0, le=120)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def _check(self) -> DatabaseSection:
        if self.pool_min > self.pool_max:
            raise ValueError("pool_min must be <= pool_max")
        return self


class RetentionSection(StrictModel):
    history_days: int = Field(default=90, ge=1, le=3650)
    events_days: int = Field(default=365, ge=1, le=3650)
    health_days: int = Field(default=30, ge=1, le=3650)
    audit_days: int = Field(default=1825, ge=30, le=36500)
    partitions_ahead_days: int = Field(default=7, ge=1, le=60)
    archive_mode: Literal["none", "csv_gzip"] = "none"
    archive_dir: str = "data/archive"
    maintenance_hour_utc: int = Field(default=2, ge=0, le=23)


class HealthSection(StrictModel):
    snapshot_interval_s: float = Field(default=30, ge=5, le=3600)


class WritesSection(StrictModel):
    default_timeout_s: float = Field(default=5.0, gt=0, le=60)
    kill_switch_env: str = "GATEWAY_WRITE_DISABLE"
    rate_limit_per_min: int = Field(default=30, ge=1, le=10_000)


class RestForwarderConfig(StrictModel):
    type: Literal["rest_forwarder"]
    name: str = Field(pattern=ID_PATTERN)
    enabled: bool = False
    url: str = Field(pattern=r"^https?://")
    token_env: str | None = None
    verify_tls: bool = True
    ca_file: str | None = None
    batch_size: int = Field(default=500, ge=1, le=10_000)
    timeout_s: float = Field(default=10, gt=0, le=120)
    outbox_path: str = "data/outbox.sqlite3"
    outbox_max_rows: int = Field(default=5_000_000, ge=1000)
    publish: list[Literal["readings", "events"]] = Field(default_factory=lambda: ["readings", "events"])
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class MqttPublisherConfig(StrictModel):
    type: Literal["mqtt_publisher"]
    name: str = Field(pattern=ID_PATTERN)
    enabled: bool = False
    host: str
    port: int = Field(default=8883, ge=1, le=65535)
    tls: bool = True
    ca_file: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    client_id: str | None = None
    topic_prefix: str = "gateway"
    qos: Literal[0, 1, 2] = 1
    queue_max_size: int = Field(default=10_000, ge=10)
    publish: list[Literal["readings", "events"]] = Field(default_factory=lambda: ["events"])


IntegrationConfig = Annotated[RestForwarderConfig | MqttPublisherConfig, Field(discriminator="type")]


class Settings(StrictModel):
    gateway: GatewaySection
    buffer: BufferSection = Field(default_factory=BufferSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    health: HealthSection = Field(default_factory=HealthSection)
    writes: WritesSection = Field(default_factory=WritesSection)
    integrations: list[IntegrationConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        names = [i.name for i in self.integrations]
        if len(names) != len(set(names)):
            raise ValueError("integration names must be unique")
        return self


class GatewayConfig(StrictModel):
    """Validated aggregate: settings + plants + tags grouped by device."""

    config_dir: Path
    settings: Settings
    plants: list[PlantConfig]
    tags: dict[str, list[TagConfig]]   # device_id -> tags (enabled and disabled)

    def devices(self, *, enabled_only: bool = False) -> list[DeviceConfig]:
        out: list[DeviceConfig] = []
        for plant in self.plants:
            for device in plant.devices:
                if enabled_only and not (plant.enabled and device.enabled):
                    continue
                out.append(device)
        return out

    def device(self, device_id: str) -> DeviceConfig | None:
        return next((d for d in self.devices() if d.device_id == device_id), None)

    def resolve_path(self, value: str) -> Path:
        """Resolve a path relative to the project root (parent of config dir)."""
        path = Path(value)
        return path if path.is_absolute() else (self.config_dir.parent / path).resolve()
