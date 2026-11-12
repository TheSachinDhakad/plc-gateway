"""Durable spool: persistence across restarts (crash recovery), ack/nack, overflow policies."""

from __future__ import annotations

from models.config import BufferSection
from models.events import EventType, TagEvent
from models.reading import Quality, TagReading
from services.buffer_service import BufferService, SpoolStore
from services.metrics import Metrics
from utils.timeutil import utc_now


def reading(i: int) -> TagReading:
    now = utc_now()
    return TagReading("p", "d", "D", f"t{i}", f"T{i}", float(i), float(i), "REAL", "float", Quality.GOOD, now, now)


def event() -> TagEvent:
    return TagEvent("p", "d", "t0", EventType.COMM_LOST, utc_now(), message="lost")


def settings(**kw) -> BufferSection:
    return BufferSection(**{"flush_interval_ms": 50, "batch_size": 100, **kw})


async def test_spool_first_survives_restart(tmp_path):
    path = tmp_path / "spool.sqlite3"
    buf = BufferService(settings(), path, Metrics())
    await buf.start()
    buf.submit([reading(i) for i in range(250)], [event()])
    await buf.stop()                              # graceful stop flushes queue to disk

    # "restart": a new instance finds every record and replays oldest first
    buf2 = BufferService(settings(), path, Metrics())
    await buf2.start()
    assert buf2.spool_rows == 251
    seen = []
    while (batch := await buf2.fetch_batch(100)) is not None:
        seen += [r.tag_id for r in batch.readings]
        await buf2.ack(batch)
    assert seen == [f"t{i}" for i in range(250)]
    assert buf2.spool_rows == 0
    await buf2.stop()


async def test_unacked_batch_is_redelivered_after_crash(tmp_path):
    path = tmp_path / "spool.sqlite3"
    buf = BufferService(settings(), path, Metrics())
    await buf.start()
    buf.submit([reading(i) for i in range(10)], [])
    await buf.flush_to_spool()
    batch = await buf.fetch_batch(10)
    assert batch is not None and len(batch.readings) == 10
    uids = [r.reading_uid for r in batch.readings]
    # simulate crash before ack: close the spool without deleting
    await buf.stop()
    buf2 = BufferService(settings(), path, Metrics())
    await buf2.start()
    again = await buf2.fetch_batch(10)
    assert [r.reading_uid for r in again.readings] == uids   # same UUIDs -> idempotent DB insert
    await buf2.stop()


async def test_memory_first_nack_goes_to_spool(tmp_path):
    buf = BufferService(settings(durability_mode="memory_first"), tmp_path / "s.sqlite3", Metrics())
    await buf.start()
    buf.submit([reading(1), reading(2)], [])
    batch = await buf.fetch_batch(10)
    assert batch.source == "memory"
    await buf.nack(batch)                       # DB failed -> durable
    assert buf.spool_rows == 2
    replay = await buf.fetch_batch(10)
    assert replay.source == "spool" and len(replay.readings) == 2
    await buf.stop()


def test_overflow_policy_keeps_events(tmp_path):
    store = SpoolStore(tmp_path / "s.sqlite3")
    store.open()
    store.append([(1, {"e": 1})] + [(0, {"r": i}) for i in range(20)] + [(1, {"e": 2})])
    dropped = store.enforce_limit(10, "drop_cyclic_keep_events")
    assert dropped == {"reading": 12}
    kinds = [k for _, k, _ in store.fetch(100)]
    assert kinds.count(1) == 2 and len(kinds) == 10
    store.close()


async def test_queue_overflow_is_counted_not_silent(tmp_path):
    metrics = Metrics()
    buf = BufferService(settings(queue_max_size=100, flush_interval_ms=60000), tmp_path / "s.sqlite3", metrics)
    await buf.start()
    buf.submit([reading(i) for i in range(250)], [])
    assert buf.queue_size == 200
    assert metrics.counter("records_dropped_total", reason="queue_overflow") == 50
    await buf.stop()
