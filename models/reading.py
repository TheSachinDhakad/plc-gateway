"""Hot-path reading/write result records."""

from __future__ import annotations

import enum
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from utils.timeutil import from_iso, to_iso, uuid7

TagValue = bool | int | float | str | list[Any]
TimestampOrigin = Literal["device", "gateway_read"]


class Quality(enum.StrEnum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"
    BAD_NOT_CONNECTED = "BAD_NOT_CONNECTED"
    BAD_COMM_FAILURE = "BAD_COMM_FAILURE"
    BAD_TIMEOUT = "BAD_TIMEOUT"
    BAD_ADDRESS = "BAD_ADDRESS"
    BAD_TYPE_MISMATCH = "BAD_TYPE_MISMATCH"
    BAD_CONFIG_ERROR = "BAD_CONFIG_ERROR"
    BAD_DEVICE_FAILURE = "BAD_DEVICE_FAILURE"

    @property
    def is_good(self) -> bool:
        return self is Quality.GOOD

    @property
    def is_bad(self) -> bool:
        return self.value.startswith("BAD")


QUALITY_CODES: dict[Quality, int] = {q: i for i, q in enumerate(Quality)}
QUALITY_FROM_CODE: dict[int, Quality] = {i: q for q, i in QUALITY_CODES.items()}


class WriteStatus(enum.StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"          # written (and verified if read-back enabled)
    MISMATCH = "MISMATCH"        # written but read-back differs
    FAILED = "FAILED"            # device rejected / definitely not written
    UNCERTAIN = "UNCERTAIN"      # outcome unknown (timeout after send) — never auto-retried
    REJECTED = "REJECTED"        # blocked by gateway policy before reaching the device


@dataclass(slots=True, frozen=True)
class RawReadResult:
    tag_id: str
    value: TagValue | None
    quality: Quality
    source_timestamp: datetime
    timestamp_origin: TimestampOrigin = "gateway_read"
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: float | None = None


@dataclass(slots=True, frozen=True)
class WriteResult:
    tag_id: str
    status: WriteStatus
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: float | None = None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"__float__": repr(value)}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _from_json_safe(value: Any) -> Any:
    if isinstance(value, dict) and "__float__" in value:
        return float(value["__float__"])
    if isinstance(value, list):
        return [_from_json_safe(v) for v in value]
    return value


@dataclass(slots=True)
class TagReading:
    plant_id: str
    device_id: str
    device_name: str
    tag_id: str
    tag_name: str
    value: TagValue | None
    raw_value: TagValue | None
    data_type: str
    value_type: str
    quality: Quality
    source_timestamp: datetime
    gateway_timestamp: datetime
    timestamp_origin: TimestampOrigin = "gateway_read"
    engineering_unit: str | None = None
    logging_mode: str = "cyclic"
    error_code: str | None = None
    reading_uid: uuid.UUID = field(default_factory=uuid7)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reading_uid": str(self.reading_uid),
            "plant_id": self.plant_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "tag_id": self.tag_id,
            "tag_name": self.tag_name,
            "value": _json_safe(self.value),
            "raw_value": _json_safe(self.raw_value),
            "data_type": self.data_type,
            "value_type": self.value_type,
            "quality": self.quality.value,
            "source_timestamp": to_iso(self.source_timestamp),
            "timestamp_origin": self.timestamp_origin,
            "gateway_timestamp": to_iso(self.gateway_timestamp),
            "engineering_unit": self.engineering_unit,
            "logging_mode": self.logging_mode,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TagReading:
        src = from_iso(data["source_timestamp"])
        gw = from_iso(data["gateway_timestamp"])
        assert src is not None and gw is not None
        return cls(
            plant_id=data["plant_id"],
            device_id=data["device_id"],
            device_name=data["device_name"],
            tag_id=data["tag_id"],
            tag_name=data["tag_name"],
            value=_from_json_safe(data["value"]),
            raw_value=_from_json_safe(data.get("raw_value")),
            data_type=data["data_type"],
            value_type=data["value_type"],
            quality=Quality(data["quality"]),
            source_timestamp=src,
            gateway_timestamp=gw,
            timestamp_origin=data.get("timestamp_origin", "gateway_read"),
            engineering_unit=data.get("engineering_unit"),
            logging_mode=data.get("logging_mode", "cyclic"),
            error_code=data.get("error_code"),
            reading_uid=uuid.UUID(data["reading_uid"]),
        )
