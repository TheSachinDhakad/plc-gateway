"""Bounded in-memory queue + durable SQLite (WAL) spool.

Durability modes
----------------
``spool_first`` (default): every flush interval the queue is appended to the spool in one
SQLite transaction (``synchronous=FULL``). The BatchWriter reads the oldest spool rows,
commits them to MySQL, then deletes them. Crash/power-loss window: only readings still in
the in-memory queue (<= ``flush_interval_ms``).

``memory_first``: the BatchWriter drains the queue directly; batches that fail to commit are
appended to the spool and replayed first. Larger loss window on power failure (all queued
data), lower disk wear.

In both modes graceful shutdown flushes the queue to the spool, and the spool is replayed on
the next start. Replays are idempotent because MySQL keys include reading/event UUIDs.

Overflow: if the queue is full, items go to a bounded overflow list flushed with priority;
if that is also full the newest items are dropped and COUNTED (metrics + log + health),
never silently. If the spool exceeds ``spool_max_rows`` the configured policy applies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from models.config import BufferSection
from models.events import TagEvent
from models.reading import TagReading
from services.metrics import Metrics
from utils.exceptions import SpoolError

log = logging.getLogger(__name__)

KIND_READING = 0
KIND_EVENT = 1
Item = tuple[int, dict[str, Any]]   # (kind, payload)


class SpoolStore:
    """Synchronous SQLite spool — only ever called from one dedicated thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE TABLE IF NOT EXISTS spool (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "kind INTEGER NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_spool_kind ON spool(kind, id)")
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise SpoolError(f"spool integrity check failed: {integrity}")
        self._conn = conn
        return self.count()

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SpoolError("spool not open")
        return self._conn

    def append(self, items: list[Item]) -> None:
        if not items:
            return
        conn = self._c()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("INSERT INTO spool (kind, created, payload) VALUES (?, ?, ?)",
                             [(k, now, json.dumps(p, separators=(",", ":"))) for k, p in items])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def fetch(self, limit: int) -> list[tuple[int, int, dict[str, Any]]]:
        rows = self._c().execute("SELECT id, kind, payload FROM spool ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(i, k, json.loads(p)) for i, k, p in rows]

    def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        conn = self._c()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                conn.execute(f"DELETE FROM spool WHERE id IN ({','.join('?' * len(chunk))})", chunk)  # noqa: S608
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def count(self) -> int:
        return int(self._c().execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def enforce_limit(self, max_rows: int, policy: str) -> dict[str, int]:
        """Apply the overflow policy; return dropped counts by kind."""
        total = self.count()
        excess = total - max_rows
        if excess <= 0 or policy == "reject_new":
            return {}
        conn = self._c()
        dropped: dict[str, int] = {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if policy == "drop_cyclic_keep_events":
                cur = conn.execute("DELETE FROM spool WHERE id IN (SELECT id FROM spool WHERE kind = ? "
                                   "ORDER BY id LIMIT ?)", (KIND_READING, excess))
                dropped["reading"] = cur.rowcount
                excess -= cur.rowcount
            if excess > 0:
                cur = conn.execute("DELETE FROM spool WHERE id IN (SELECT id FROM spool ORDER BY id LIMIT ?)",
                                   (excess,))
                dropped["any"] = cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return dropped

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
            self._conn = None


@dataclass(slots=True)
class Batch:
    readings: list[TagReading]
    events: list[TagEvent]
    source: Literal["spool", "memory"]
    spool_ids: list[int] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.readings) + len(self.events)


def _decode(kind: int, payload: dict[str, Any], readings: list[TagReading], events: list[TagEvent]) -> None:
    if kind == KIND_READING:
        readings.append(TagReading.from_dict(payload))
    else:
        events.append(TagEvent.from_dict(payload))


class BufferService:
    def __init__(self, settings: BufferSection, spool_path: Path, metrics: Metrics) -> None:
        self.settings = settings
        self.metrics = metrics
        self.spool = SpoolStore(spool_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spool")
        self._queue: deque[Item] = deque()
        self._overflow: deque[Item] = deque()
        self._data_event = asyncio.Event()
        self._flush_now = asyncio.Event()
        self._stop = asyncio.Event()
        self._flusher: asyncio.Task[None] | None = None
        self.spool_rows = 0
        self._last_drop_log = 0.0
        self._started = False

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def start(self) -> None:
        self.spool_rows = await self._run(self.spool.open)
        self.metrics.set_gauge("spool_rows", self.spool_rows)
        if self.spool_rows:
            log.warning("spool contains %d records from a previous run; replaying", self.spool_rows)
            self._data_event.set()
        self._started = True
        self._flusher = asyncio.create_task(self._flush_loop(), name="spool-flusher")

    # ------------------------------------------------------------------ producer side (never blocks)
    @property
    def queue_size(self) -> int:
        return len(self._queue) + len(self._overflow)

    def submit(self, readings: list[TagReading], events: list[TagEvent]) -> None:
        items: list[Item] = [(KIND_EVENT, e.to_dict()) for e in events]
        items += [(KIND_READING, r.to_dict()) for r in readings]
        cap = self.settings.queue_max_size
        for item in items:
            if len(self._queue) < cap:
                self._queue.append(item)
            elif len(self._overflow) < cap:
                self._overflow.append(item)
                self._flush_now.set()
            else:
                self._count_drop("queue_overflow", 1)
        self.metrics.set_gauge("queue_size", self.queue_size)
        if len(self._queue) >= self.settings.batch_size:
            self._flush_now.set()
        self._data_event.set()

    def _count_drop(self, reason: str, n: int) -> None:
        self.metrics.inc("records_dropped_total", n, reason=reason)
        now = time.monotonic()
        if now - self._last_drop_log > 10:
            self._last_drop_log = now
            log.error("DATA DROPPED: %d record(s), reason=%s (total=%d)", n, reason,
                      self.metrics.counter_total("records_dropped_total"), extra={"error_code": "DATA_DROPPED"})

    def _drain_memory(self, limit: int | None = None) -> list[Item]:
        out: list[Item] = []
        for source in (self._overflow, self._queue):
            while source and (limit is None or len(out) < limit):
                out.append(source.popleft())
        self.metrics.set_gauge("queue_size", self.queue_size)
        return out

    # ------------------------------------------------------------------ spool flushing
    async def _append_to_spool(self, items: list[Item]) -> None:
        if not items:
            return
        policy = self.settings.overflow_policy
        if policy == "reject_new" and self.spool_rows + len(items) > self.settings.spool_max_rows:
            allowed = max(0, self.settings.spool_max_rows - self.spool_rows)
            self._count_drop("spool_full_reject_new", len(items) - allowed)
            items = items[:allowed]
        t0 = time.perf_counter()
        await self._run(self.spool.append, items)
        self.metrics.observe("spool_append_ms", (time.perf_counter() - t0) * 1000)
        self.spool_rows += len(items)
        if self.spool_rows > self.settings.spool_max_rows:
            dropped = await self._run(self.spool.enforce_limit, self.settings.spool_max_rows, policy)
            for kind, n in dropped.items():
                if n:
                    self._count_drop(f"spool_full_{kind}", n)
            self.spool_rows = await self._run(self.spool.count)
        self.metrics.set_gauge("spool_rows", self.spool_rows)

    async def flush_to_spool(self) -> None:
        items = self._drain_memory()
        try:
            await self._append_to_spool(items)
        except Exception:
            # put back (front) so nothing is lost; retried next cycle
            self._queue.extendleft(reversed(items))
            raise

    async def _flush_loop(self) -> None:
        interval = self.settings.flush_interval_ms / 1000
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._flush_now.wait(), interval)
            self._flush_now.clear()
            try:
                if self.settings.durability_mode == "spool_first" or self._overflow:
                    await self.flush_to_spool()
                    if self.spool_rows:
                        self._data_event.set()
            except Exception:
                log.exception("spool flush failed; data kept in memory and retried")
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ consumer side (BatchWriter)
    async def wait_for_data(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._data_event.wait(), timeout)
        self._data_event.clear()

    async def fetch_batch(self, limit: int) -> Batch | None:
        if self.spool_rows > 0:
            rows = await self._run(self.spool.fetch, limit)
            if rows:
                readings: list[TagReading] = []
                events: list[TagEvent] = []
                ids = []
                for row_id, kind, payload in rows:
                    ids.append(row_id)
                    try:
                        _decode(kind, payload, readings, events)
                    except Exception as exc:   # corrupt row: count and skip (it is deleted on ack)
                        self._count_drop("spool_corrupt", 1)
                        log.error("corrupt spool row %d skipped: %s", row_id, exc)
                return Batch(readings, events, "spool", spool_ids=ids)
            self.spool_rows = 0
        if self.settings.durability_mode == "memory_first":
            items = self._drain_memory(limit)
            if items:
                readings, events = [], []
                for kind, payload in items:
                    _decode(kind, payload, readings, events)
                return Batch(readings, events, "memory", items=items)
        return None

    async def ack(self, batch: Batch) -> None:
        if batch.source == "spool":
            await self._run(self.spool.delete, batch.spool_ids)
            self.spool_rows = max(0, self.spool_rows - len(batch.spool_ids))
            self.metrics.set_gauge("spool_rows", self.spool_rows)

    async def nack(self, batch: Batch) -> None:
        if batch.source == "memory":
            await self._append_to_spool(batch.items)

    # ------------------------------------------------------------------ shutdown
    async def stop(self) -> None:
        self._stop.set()
        self._flush_now.set()
        if self._flusher:
            await asyncio.gather(self._flusher, return_exceptions=True)
        if self._started:
            try:
                await self.flush_to_spool()
            except Exception:
                log.exception("final spool flush failed; %d in-memory records lost", self.queue_size)
                self._count_drop("shutdown_flush_failed", self.queue_size)
            await self._run(self.spool.close)
        self._executor.shutdown(wait=True)
