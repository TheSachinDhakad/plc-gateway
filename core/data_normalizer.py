"""Convert driver RawReadResults into normalized TagReadings (scaling, units, typing)."""

from __future__ import annotations

from datetime import datetime

from drivers.codecs.datatypes import value_type_of
from models.plc import DeviceConfig
from models.reading import Quality, RawReadResult, TagReading, TagValue
from models.tag import TagConfig
from utils.timeutil import utc_now


class DataNormalizer:
    def normalize(self, device: DeviceConfig, tag: TagConfig, raw: RawReadResult,
                  gateway_timestamp: datetime | None = None) -> TagReading:
        value: TagValue | None = raw.value
        quality = raw.quality
        error_code = raw.error_code
        if quality.is_bad:
            value = None
        elif tag.scaling is not None and value is not None:
            if isinstance(value, bool) or not isinstance(value, int | float):
                value, quality, error_code = None, Quality.BAD_TYPE_MISMATCH, "SCALING_NON_NUMERIC"
            else:
                value = tag.scaling.apply(float(value))
        return TagReading(
            plant_id=device.plant_id,
            device_id=device.device_id,
            device_name=device.name,
            tag_id=tag.tag_id,
            tag_name=tag.name,
            value=value,
            raw_value=raw.value if not raw.quality.is_bad else None,
            data_type=tag.data_type.value,
            value_type=value_type_of(value),
            quality=quality,
            source_timestamp=raw.source_timestamp,
            gateway_timestamp=gateway_timestamp or utc_now(),
            timestamp_origin=raw.timestamp_origin,
            engineering_unit=tag.engineering_unit,
            logging_mode=tag.logging_mode.value,
            error_code=error_code,
        )

    def not_connected(self, device: DeviceConfig, tag: TagConfig, reason: str) -> TagReading:
        now = utc_now()
        raw = RawReadResult(tag.tag_id, None, Quality.BAD_NOT_CONNECTED, now, "gateway_read",
                            "NOT_CONNECTED", reason)
        return self.normalize(device, tag, raw, now)
