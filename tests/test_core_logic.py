"""Event detector, scheduler, backoff/circuit breaker, normalizer, validators (pure)."""

from __future__ import annotations

import pytest

from core.data_normalizer import DataNormalizer
from core.event_detector import EventDetector
from core.polling_scheduler import PollingScheduler
from models.events import EventType
from models.reading import Quality, RawReadResult
from tests.conftest import make_device, make_tag
from utils.backoff import Backoff, CircuitBreaker, CircuitState
from utils.exceptions import WriteRejectedError
from utils.timeutil import utc_now, uuid7
from utils.validators import check_engineering_limits, coerce_write_value

DEVICE = make_device("modbus_tcp", {"host": "127.0.0.1"})
NORM = DataNormalizer()


def reading(tag, value, quality=Quality.GOOD):
    raw = RawReadResult(tag.tag_id, value, quality, utc_now())
    return NORM.normalize(DEVICE, tag, raw)


# ---------------------------------------------------------------------------------------- events

def test_event_mode_logs_only_on_change_and_no_duplicates():
    tag = make_tag("t", "holding:0", "INT", logging_mode="event")
    det = EventDetector()
    store, ev = det.process(tag, reading(tag, 5))
    assert store and ev == []                       # baseline, no change event
    for _ in range(3):
        store, ev = det.process(tag, reading(tag, 5))
        assert not store and ev == []               # unchanged -> nothing
    store, ev = det.process(tag, reading(tag, 6))
    assert store and [e.event_type for e in ev] == [EventType.VALUE_CHANGE]
    assert ev[0].previous_value == 5 and ev[0].new_value == 6


def test_deadband_absolute_tracks_drift_against_reference():
    tag = make_tag("t", "holding:0", "REAL", logging_mode="event", deadband={"type": "absolute", "value": 1.0})
    det = EventDetector()
    det.process(tag, reading(tag, 10.0))
    changes = [det.process(tag, reading(tag, v))[0] for v in (10.4, 10.8, 11.2)]
    assert changes == [False, False, True]          # slow drift detected once it exceeds deadband


def test_deadband_percent():
    tag = make_tag("t", "holding:0", "REAL", logging_mode="both", deadband={"type": "percent", "value": 10})
    det = EventDetector()
    det.process(tag, reading(tag, 100.0))
    _, ev = det.process(tag, reading(tag, 105.0))
    assert ev == []
    _, ev = det.process(tag, reading(tag, 111.0))
    assert [e.event_type for e in ev] == [EventType.VALUE_CHANGE]


def test_cyclic_always_stored():
    tag = make_tag("t", "holding:0", "INT")
    det = EventDetector()
    assert all(det.process(tag, reading(tag, 1))[0] for _ in range(3))


def test_edges():
    tag = make_tag("b", "coil:0", "BOOL", logging_mode="event", edge_events=True)
    det = EventDetector()
    det.process(tag, reading(tag, False))
    _, ev = det.process(tag, reading(tag, True))
    assert EventType.RISING_EDGE in [e.event_type for e in ev]
    _, ev = det.process(tag, reading(tag, True))
    assert ev == []
    _, ev = det.process(tag, reading(tag, False))
    assert EventType.FALLING_EDGE in [e.event_type for e in ev]


def test_thresholds_with_hysteresis():
    tag = make_tag("t", "holding:0", "REAL", thresholds={"high": 80, "low": 10, "hysteresis": 2})
    det = EventDetector()
    seq = [50, 81, 85, 79, 77, 9, 11, 13]
    types = []
    for v in seq:
        types += [e.event_type for e in det.process(tag, reading(tag, float(v)))[1]]
    assert types == [EventType.THRESHOLD_HIGH, EventType.THRESHOLD_CLEAR, EventType.THRESHOLD_LOW,
                     EventType.THRESHOLD_CLEAR]


def test_quality_change_events_once():
    tag = make_tag("t", "holding:0", "INT", logging_mode="event")
    det = EventDetector()
    det.process(tag, reading(tag, 1))
    store, ev = det.process(tag, reading(tag, None, Quality.BAD_NOT_CONNECTED))
    assert store and [e.event_type for e in ev] == [EventType.QUALITY_CHANGE]
    store, ev = det.process(tag, reading(tag, None, Quality.BAD_NOT_CONNECTED))
    assert not store and ev == []
    store, ev = det.process(tag, reading(tag, 1))
    assert store and [e.event_type for e in ev] == [EventType.QUALITY_CHANGE]   # value unchanged


# ---------------------------------------------------------------------------------------- normalizer

def test_normalizer_scaling_and_bad_quality():
    tag = make_tag("t", "holding:0", "UINT", scaling={"raw_min": 0, "raw_max": 27648, "eng_min": 0,
                                                      "eng_max": 100})
    r = reading(tag, 13824)
    assert r.value == pytest.approx(50.0) and r.raw_value == 13824 and r.value_type == "float"
    bad = reading(tag, None, Quality.BAD_TIMEOUT)
    assert bad.value is None and bad.value_type == "null"
    as_bool = reading(make_tag("b", "coil:0", "BOOL"), True)
    assert as_bool.value is True and as_bool.value_type == "bool"


# ---------------------------------------------------------------------------------------- scheduler

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_scheduler_drift_free_and_overruns():
    clock = FakeClock()
    t1 = make_tag("a", "holding:0", "INT", poll_interval_ms=1000)
    t2 = make_tag("b", "holding:1", "INT", poll_interval_ms=60000)
    sched = PollingScheduler({1000: [t1], 60000: [t2]}, clock=clock)
    due, _ = sched.next_due()
    assert {sc.interval_ms for sc in due} == {1000, 60000}
    for sc in due:
        sched.mark_started(sc)
        clock.t += 0.1
        sched.mark_done(sc)
    due, wait = sched.next_due()
    assert due == [] and wait == pytest.approx(0.8)
    clock.t = 1001.0
    fast = sched.next_due()[0][0]
    sched.mark_started(fast)
    clock.t = 1004.5            # cycle took 3.5 s -> 3 deadlines missed
    skipped = sched.mark_done(fast)
    assert skipped == 3 and fast.overruns == 3
    assert fast.next_due == pytest.approx(1005.0)   # still aligned to the 1 s grid


# ---------------------------------------------------------------------------------------- backoff / breaker

def test_backoff_growth_and_cap():
    b = Backoff(1, 8, jitter=0)
    assert [b.next_delay() for _ in range(6)] == [1, 2, 4, 8, 8, 8]
    b.reset()
    assert b.next_delay() == 1


def test_circuit_breaker_transitions():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, open_duration_s=10, clock=clock)
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    cb.record_failure()
    assert cb.state is CircuitState.OPEN and not cb.allow_request()
    clock.t += 10
    assert cb.state is CircuitState.HALF_OPEN and cb.allow_request()
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    clock.t += 10
    cb.record_success()
    assert cb.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------------------- validators / ids

@pytest.mark.parametrize("dtype,value,expected", [
    ("REAL", 5, 5.0), ("INT", 5.0, 5), ("BOOL", 1, True), ("STRING", "abc", "abc"),
])
def test_coerce_ok(dtype, value, expected):
    from drivers.codecs.datatypes import DataType
    assert coerce_write_value(DataType(dtype), value, string_length=10) == expected


@pytest.mark.parametrize("dtype,value", [
    ("INT", "5"), ("INT", 5.5), ("INT", True), ("INT", 40000), ("REAL", "1.0"), ("REAL", float("inf")),
    ("BOOL", "true"), ("BOOL", 2), ("STRING", 5),
])
def test_coerce_rejects(dtype, value):
    from drivers.codecs.datatypes import DataType
    with pytest.raises(WriteRejectedError):
        coerce_write_value(DataType(dtype), value)


def test_engineering_limits():
    check_engineering_limits(50, 0, 100)
    with pytest.raises(WriteRejectedError):
        check_engineering_limits(101, 0, 100)
    with pytest.raises(WriteRejectedError):
        check_engineering_limits([1, -1], 0, 100)


def test_uuid7_is_time_ordered_and_versioned():
    ids = [uuid7() for _ in range(2000)]
    assert all(i.version == 7 for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
