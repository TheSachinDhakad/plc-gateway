"""Change/edge/threshold/quality event detection with deadband and duplicate suppression.

Rules (per tag):

* First reading: logged (establishes a baseline); no VALUE_CHANGE event.
* Quality change: QUALITY_CHANGE event; the reading is logged (unless logging_mode none).
* Significant change (GOOD -> GOOD): numeric values are compared with the deadband against
  the *reference* value — the value at the last significant change — so slow drift is still
  captured; other types use equality.
  ``event``/``both`` modes emit VALUE_CHANGE; ``event`` mode logs only on change.
* BOOL edges (edge_events: true): RISING_EDGE / FALLING_EDGE vs the previous GOOD value.
* Thresholds: state machine normal/high/low with hysteresis -> THRESHOLD_HIGH/LOW/CLEAR.
* Percent deadband is relative to |reference value| (0 -> any change is significant).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from drivers.codecs.datatypes import DataType
from models.events import EventType, TagEvent
from models.reading import Quality, TagReading
from models.tag import LoggingMode, TagConfig


@dataclass(slots=True)
class _TagState:
    last_value: Any = None
    last_quality: Quality | None = None
    reference: Any = None
    has_reference: bool = False
    threshold_state: str = "normal"   # normal | high | low


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


class EventDetector:
    def __init__(self) -> None:
        self._state: dict[tuple[str, str], _TagState] = {}

    def reset(self, device_id: str | None = None) -> None:
        if device_id is None:
            self._state.clear()
        else:
            for key in [k for k in self._state if k[0] == device_id]:
                del self._state[key]

    def _significant(self, tag: TagConfig, old: Any, new: Any) -> bool:
        if old is None or new is None:
            return old is not new
        if isinstance(old, list) or isinstance(new, list):
            if not (isinstance(old, list) and isinstance(new, list)) or len(old) != len(new):
                return True
            return any(self._significant(tag, o, n) for o, n in zip(old, new, strict=True))
        if _is_number(old) and _is_number(new):
            if isinstance(old, float) and isinstance(new, float) and math.isnan(old) and math.isnan(new):
                return False
            db = tag.deadband
            if db is None or db.value == 0:
                return old != new
            diff = abs(new - old)
            if db.type == "absolute":
                return diff > db.value
            base = abs(old)
            return diff > 0 if base == 0 else diff / base * 100.0 > db.value
        return old != new

    def _thresholds(self, tag: TagConfig, state: _TagState, reading: TagReading) -> list[TagEvent]:
        th = tag.thresholds
        value = reading.value
        if th is None or not _is_number(value):
            return []
        assert isinstance(value, int | float)
        events: list[TagEvent] = []
        prev = state.threshold_state
        new = prev
        if prev == "normal":
            if th.high is not None and value >= th.high:
                new = "high"
            elif th.low is not None and value <= th.low:
                new = "low"
        elif prev == "high":
            if th.high is not None and value < th.high - th.hysteresis:
                new = "normal"
                if th.low is not None and value <= th.low:
                    new = "low"
        elif prev == "low" and th.low is not None and value > th.low + th.hysteresis:
            new = "normal"
            if th.high is not None and value >= th.high:
                new = "high"
        if new != prev:
            if new == "high":
                events.append(self._event(reading, EventType.THRESHOLD_HIGH, None, value, threshold=th.high,
                                          message=f"value {value} >= high {th.high}"))
            elif new == "low":
                events.append(self._event(reading, EventType.THRESHOLD_LOW, None, value, threshold=th.low,
                                          message=f"value {value} <= low {th.low}"))
            else:
                events.append(self._event(reading, EventType.THRESHOLD_CLEAR, None, value,
                                          threshold=th.high if prev == "high" else th.low,
                                          message=f"value {value} back within limits"))
            state.threshold_state = new
        return events

    @staticmethod
    def _event(reading: TagReading, etype: EventType, prev: Any, new: Any, *, threshold: float | None = None,
               message: str | None = None, prev_q: str | None = None, new_q: str | None = None) -> TagEvent:
        return TagEvent(plant_id=reading.plant_id, device_id=reading.device_id, tag_id=reading.tag_id,
                        event_type=etype, event_timestamp=reading.gateway_timestamp, previous_value=prev,
                        new_value=new, previous_quality=prev_q, new_quality=new_q, threshold=threshold,
                        message=message)

    def process(self, tag: TagConfig, reading: TagReading) -> tuple[bool, list[TagEvent]]:
        """Return (should_store_in_history, events)."""
        key = (reading.device_id, reading.tag_id)
        state = self._state.get(key)
        first = state is None
        if state is None:
            state = self._state[key] = _TagState()
        events: list[TagEvent] = []
        mode = tag.logging_mode

        quality_changed = not first and state.last_quality is not reading.quality
        if quality_changed:
            events.append(self._event(reading, EventType.QUALITY_CHANGE, state.last_value, reading.value,
                                      prev_q=state.last_quality.value if state.last_quality else None,
                                      new_q=reading.quality.value,
                                      message=f"quality {state.last_quality} -> {reading.quality}"))

        changed = False
        if reading.quality is Quality.GOOD or reading.quality is Quality.UNCERTAIN:
            changed = (not state.has_reference) or self._significant(tag, state.reference, reading.value)
            if changed:
                if state.has_reference and mode in (LoggingMode.EVENT, LoggingMode.BOTH):
                    events.append(self._event(reading, EventType.VALUE_CHANGE, state.reference, reading.value))
                state.reference = reading.value
                state.has_reference = True
            if tag.edge_events and tag.data_type is DataType.BOOL and isinstance(reading.value, bool) \
                    and isinstance(state.last_value, bool) and state.last_quality is Quality.GOOD:
                if not state.last_value and reading.value:
                    events.append(self._event(reading, EventType.RISING_EDGE, False, True))
                elif state.last_value and not reading.value:
                    events.append(self._event(reading, EventType.FALLING_EDGE, True, False))
            if reading.quality is Quality.GOOD:
                events.extend(self._thresholds(tag, state, reading))

        if mode is LoggingMode.NONE:
            store = False
        elif mode is LoggingMode.EVENT:
            store = first or changed or quality_changed
        else:
            store = True

        state.last_value = reading.value if not reading.quality.is_bad else state.last_value
        state.last_quality = reading.quality
        return store, events
