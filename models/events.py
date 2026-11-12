"""Event records produced by the event detector and connection manager."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from models.reading import _from_json_safe, _json_safe
from utils.timeutil import from_iso, to_iso, uuid7


class EventType(enum.StrEnum):
    VALUE_CHANGE = "VALUE_CHANGE"
    RISING_EDGE = "RISING_EDGE"
    FALLING_EDGE = "FALLING_EDGE"
    THRESHOLD_HIGH = "THRESHOLD_HIGH"
    THRESHOLD_LOW = "THRESHOLD_LOW"
    THRESHOLD_CLEAR = "THRESHOLD_CLEAR"
    QUALITY_CHANGE = "QUALITY_CHANGE"
    COMM_LOST = "COMM_LOST"
    COMM_RESTORED = "COMM_RESTORED"


@dataclass(slots=True)
class TagEvent:
    plant_id: str
    device_id: str
    tag_id: str | None             # None for device-level events (COMM_*)
    event_type: EventType
    event_timestamp: datetime
    previous_value: Any = None
    new_value: Any = None
    previous_quality: str | None = None
    new_quality: str | None = None
    threshold: float | None = None
    message: str | None = None
    event_uid: uuid.UUID = field(default_factory=uuid7)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_uid": str(self.event_uid),
            "plant_id": self.plant_id,
            "device_id": self.device_id,
            "tag_id": self.tag_id,
            "event_type": self.event_type.value,
            "event_timestamp": to_iso(self.event_timestamp),
            "previous_value": _json_safe(self.previous_value),
            "new_value": _json_safe(self.new_value),
            "previous_quality": self.previous_quality,
            "new_quality": self.new_quality,
            "threshold": self.threshold,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TagEvent:
        ts = from_iso(data["event_timestamp"])
        assert ts is not None
        return cls(
            plant_id=data["plant_id"],
            device_id=data["device_id"],
            tag_id=data.get("tag_id"),
            event_type=EventType(data["event_type"]),
            event_timestamp=ts,
            previous_value=_from_json_safe(data.get("previous_value")),
            new_value=_from_json_safe(data.get("new_value")),
            previous_quality=data.get("previous_quality"),
            new_quality=data.get("new_quality"),
            threshold=data.get("threshold"),
            message=data.get("message"),
            event_uid=uuid.UUID(data["event_uid"]),
        )
