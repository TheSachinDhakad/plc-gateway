"""Drain the buffer into MySQL in batches with retry, backoff and registry recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from database.repository import Repository
from services.buffer_service import BufferService
from services.metrics import Metrics
from utils.backoff import Backoff
from utils.exceptions import DatabaseError, DatabaseUnavailableError

log = logging.getLogger(__name__)


class BatchWriter:
    def __init__(
        self,
        buffer: BufferService,
        repository: Repository,
        metrics: Metrics,
        *,
        batch_size: int,
        flush_interval_s: float,
        backoff: Backoff,
        ensure_ready: Callable[[], Awaitable[None]],
    ) -> None:
        self.buffer = buffer
        self.repo = repository
        self.metrics = metrics
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.backoff = backoff
        self._ensure_ready = ensure_ready
        self.db_ok = False
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.repo.registry_ready:
                try:
                    await self._ensure_ready()
                    log.info("database ready (registry synchronized)")
                    self.backoff.reset()
                except Exception as exc:
                    self._fail(exc)
                    await self._wait(stop, self.backoff.next_delay())
                    continue
            batch = await self.buffer.fetch_batch(self.batch_size)
            if batch is None:
                await self.buffer.wait_for_data(self.flush_interval_s)
                continue
            if batch.size == 0:            # only corrupt rows: just remove them
                await self.buffer.ack(batch)
                continue
            t0 = time.perf_counter()
            try:
                history, events, skipped = await self.repo.write_batch(batch.readings, batch.events)
            except DatabaseError as exc:
                self._fail(exc)
                if isinstance(exc, DatabaseUnavailableError):
                    await self._reset_connection()
                try:
                    await self.buffer.nack(batch)
                except Exception:
                    log.exception("could not return failed batch to spool")
                    self.metrics.inc("records_dropped_total", batch.size, reason="nack_failed")
                await self._wait(stop, self.backoff.next_delay())
                continue
            duration = (time.perf_counter() - t0) * 1000
            await self.buffer.ack(batch)
            self.db_ok = True
            self.last_error = None
            self.last_success_at = time.time()
            self.backoff.reset()
            self.metrics.observe("db_insert_duration_ms", duration)
            self.metrics.inc("records_written_total", history)
            self.metrics.inc("events_written_total", events)
            self.metrics.set_gauge("db_available", 1)
            if skipped:
                # tags removed from config after the data was spooled
                self.metrics.inc("records_dropped_total", skipped, reason="unknown_tag")
                log.warning("%d records referenced unknown tags/devices and were not stored", skipped)
            if batch.source == "spool" and self.buffer.spool_rows == 0:
                log.debug("spool drained")
        log.info("batch writer stopped")

    async def _reset_connection(self) -> None:
        """Server unreachable/restarted: drop the pool so ensure_ready() rebuilds it and re-syncs."""
        try:
            await self.repo.db.close()
        except Exception as exc:
            log.debug("pool close failed: %s", exc)
        self.repo.device_pks = {}
        self.repo.tag_pks = {}

    def _fail(self, exc: Exception) -> None:
        if self.db_ok or self.last_error is None:
            log.error("MySQL write failed, data retained in spool: %s", exc,
                      extra={"operation": "db_write", "error_code": getattr(exc, "code", "DB")})
        self.db_ok = False
        self.last_error = str(exc)
        self.metrics.inc("db_write_failures_total")
        self.metrics.set_gauge("db_available", 0)
