"""Tag configuration models."""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drivers.codecs.datatypes import DataType, is_numeric

ID_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,63}$"


class LoggingMode(enum.StrEnum):
    CYCLIC = "cyclic"     # every sample is stored
    EVENT = "event"       # stored only on significant change / quality change
    BOTH = "both"         # every sample stored AND change events generated
    NONE = "none"         # only latest value is kept (no history)


class TagAccess(enum.StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


class ScalingConfig(StrictModel):
    raw_min: float
    raw_max: float
    eng_min: float
    eng_max: float
    clamp: bool = True

    @model_validator(mode="after")
    def _check_span(self) -> ScalingConfig:
        if self.raw_max == self.raw_min:
            raise ValueError("scaling raw_min and raw_max must differ")
        if self.eng_max == self.eng_min:
            raise ValueError("scaling eng_min and eng_max must differ")
        return self

    def apply(self, raw: float) -> float:
        value = self.eng_min + (raw - self.raw_min) * (self.eng_max - self.eng_min) / (self.raw_max - self.raw_min)
        if self.clamp:
            lo, hi = sorted((self.eng_min, self.eng_max))
            value = min(max(value, lo), hi)
        return value

    def invert(self, eng: float) -> float:
        return self.raw_min + (eng - self.eng_min) * (self.raw_max - self.raw_min) / (self.eng_max - self.eng_min)


class DeadbandConfig(StrictModel):
    type: Literal["absolute", "percent"] = "absolute"
    value: float = Field(ge=0)


class ThresholdConfig(StrictModel):
    high: float | None = None
    low: float | None = None
    hysteresis: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> ThresholdConfig:
        if self.high is None and self.low is None:
            raise ValueError("thresholds require high and/or low")
        if self.high is not None and self.low is not None and self.low >= self.high:
            raise ValueError("threshold low must be < high")
        return self


class TagWriteConfig(StrictModel):
    min: float | None = None
    max: float | None = None
    allowed_roles: list[str] = Field(default_factory=lambda: ["operator"])
    verify_readback: bool = True
    readback_tolerance: float = Field(default=1e-6, ge=0)
    timeout_s: float | None = Field(default=None, gt=0, le=60)

    @model_validator(mode="after")
    def _check(self) -> TagWriteConfig:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("write.min must be <= write.max")
        if not self.allowed_roles:
            raise ValueError("write.allowed_roles must not be empty")
        return self


class TagConfig(StrictModel):
    tag_id: str = Field(pattern=ID_PATTERN)
    device_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    group: str | None = Field(default=None, max_length=64)
    address: str = Field(min_length=1, max_length=256)
    data_type: DataType
    array_length: int | None = Field(default=None, ge=1, le=1000)
    string_length: int | None = Field(default=None, ge=1, le=1024)
    poll_interval_ms: int = Field(default=1000, ge=100, le=86_400_000)
    enabled: bool = True
    logging_mode: LoggingMode = LoggingMode.CYCLIC
    engineering_unit: str | None = Field(default=None, max_length=32)
    scaling: ScalingConfig | None = None
    deadband: DeadbandConfig | None = None
    thresholds: ThresholdConfig | None = None
    edge_events: bool = False
    access: TagAccess = TagAccess.READ_ONLY
    write: TagWriteConfig | None = None

    @field_validator("data_type", mode="before")
    @classmethod
    def _upper_type(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> TagConfig:
        numeric = is_numeric(self.data_type)
        if self.scaling and (not numeric or self.array_length):
            raise ValueError("scaling is only valid for numeric scalar tags")
        if self.thresholds and (not numeric or self.array_length):
            raise ValueError("thresholds are only valid for numeric scalar tags")
        if self.deadband and not numeric:
            raise ValueError("deadband is only valid for numeric tags")
        if self.edge_events and (self.data_type is not DataType.BOOL or self.array_length):
            raise ValueError("edge_events requires a scalar BOOL tag")
        if self.access is TagAccess.READ_WRITE:
            if self.write is None:
                self.write = TagWriteConfig()
        elif self.write is not None:
            raise ValueError("write block requires access: read_write")
        if self.write and (self.write.min is not None or self.write.max is not None) and not numeric:
            raise ValueError("write.min/max only valid for numeric tags")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.device_id, self.tag_id)


class TagFile(StrictModel):
    tags: list[TagConfig] = Field(default_factory=list)
