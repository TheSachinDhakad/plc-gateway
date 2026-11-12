"""One asyncio task per device: connect, poll scan classes, normalize, detect, buffer.

Device-level isolation: an exception in one worker never affects another worker. All driver
I/O for a device (polling and API writes) is serialized through ``self.io_lock``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Protocol

from core.connection_manager import ConnectionManager, sleep_or_stop
from core.data_normalizer import DataNormalizer
from core.event_detector import EventDetector
from core.polling_scheduler import PollingScheduler, ScanClass
from core.tag_manager import TagManager
from drivers.base import BaseDriver
from models.events import EventType, TagEvent
from models.plc import DeviceConfig
from models.reading import RawReadResult, TagReading, TagValue, WriteResult
from models.tag import TagConfig
from services.metrics import Metrics
from utils.exceptions import DriverConnectionError
from utils.logger import bind_context
from utils.timeutil import utc_now

log = logging.getLogger(__name__)


class ReadingSink(Protocol):
    def submit(self, readings: list[TagReading], events: list[TagEvent]) -> None: ...


class DeviceWorker:
    def __init__(
        self,
        device: DeviceConfig,
        driver: BaseDriver,
        tag_manager: TagManager,
        sink: ReadingSink,
        metrics: Metrics,
        *,
        normalizer: DataNormalizer | None = None,
        detector: EventDetector | None = None,
        on_event: list | None = None,
    ) -> None:
        self.device = device
        self.driver = driver
        self.tags = tag_manager
        self.sink = sink
        self.metrics = metrics
        self.normalizer = normalizer or DataNormalizer()
        self.detector = detector or EventDetector()
        self.io_lock = asyncio.Lock()
        self.connection = ConnectionManager(device, driver, metrics, on_lost=self._on_lost,
                                            on_restored=self._on_restored)
        self.scheduler = PollingScheduler(tag_manager.scan_classes(device.device_id),
                                          start_offset_s=random.uniform(0, 0.25))
        self._enabled_tags = tag_manager.tags_for_device(device.device_id, enabled_only=True)
        self._stop = asyncio.Event()
        self.last_poll_at = None

    # ------------------------------------------------------------------ events
    def _device_event(self, etype: EventType, message: str) -> TagEvent:
        return TagEvent(plant_id=self.device.plant_id, device_id=self.device.device_id, tag_id=None,
                        event_type=etype, event_timestamp=utc_now(), message=message)

    async def _on_lost(self, reason: str) -> None:
        readings = [self.normalizer.not_connected(self.device, t, reason) for t in self._enabled_tags]
        self._process(readings, extra_events=[self._device_event(EventType.COMM_LOST, reason[:500])])
        self.metrics.inc("comm_lost_total", device=self.device.device_id)

    async def _on_restored(self, message: str) -> None:
        self._process([], extra_events=[self._device_event(EventType.COMM_RESTORED, message)])
        self.scheduler.reset()

    # ------------------------------------------------------------------ pipeline
    def _process(self, readings: list[TagReading], extra_events: list[TagEvent] | None = None) -> None:
        to_store: list[TagReading] = []
        events: list[TagEvent] = list(extra_events or [])
        for reading in readings:
            tag = self.tags.tag(reading.device_id, reading.tag_id)
            if tag is None:
                continue
            store, tag_events = self.detector.process(tag, reading)
            if store:
                to_store.append(reading)
            events.extend(tag_events)
        self.tags.update_latest(readings)
        if events:
            self.metrics.inc("events_total", len(events), device=self.device.device_id)
        if to_store or events:
            self.sink.submit(to_store, events)

    def _normalize(self, tags: list[TagConfig], results: list[RawReadResult]) -> list[TagReading]:
        now = utc_now()
        by_id = {t.tag_id: t for t in tags}
        out = []
        ok = failed = 0
        for raw in results:
            tag = by_id[raw.tag_id]
            out.append(self.normalizer.normalize(self.device, tag, raw, now))
            if raw.quality.is_bad:
                failed += 1
            else:
                ok += 1
        if ok:
            self.metrics.inc("reads_ok_total", ok, device=self.device.device_id)
        if failed:
            self.metrics.inc("reads_failed_total", failed, device=self.device.device_id)
        return out

    async def _poll(self, sc: ScanClass) -> bool:
        """Poll one scan class. Returns False if the connection failed."""
        self.scheduler.mark_started(sc)
        t0 = time.perf_counter()
        try:
            async with self.io_lock:
                results = await self.driver.read_tags(sc.tags)
        except DriverConnectionError as exc:
            await self.connection.handle_failure(str(exc))
            return False
        except Exception as exc:
            log.exception("unexpected driver error during poll", extra={"device_id": self.device.device_id})
            await self.connection.handle_failure(f"{type(exc).__name__}: {exc}")
            return False
        duration = (time.perf_counter() - t0) * 1000
        self.metrics.observe("poll_duration_ms", duration, device=self.device.device_id)
        self.connection.breaker.record_success()
        self._process(self._normalize(sc.tags, results))
        skipped = self.scheduler.mark_done(sc)
        if skipped:
            self.metrics.inc("scan_overruns_total", skipped, device=self.device.device_id)
            log.warning("scan class %d ms overran (%.0f ms), skipped %d cycle(s)", sc.interval_ms,
                        sc.last_duration_ms, skipped, extra={"device_id": self.device.device_id})
        self.last_poll_at = utc_now()
        return True

    async def run(self) -> None:
        bind_context(plant_id=self.device.plant_id, device_id=self.device.device_id,
                     driver=str(self.device.driver))
        await self.driver.register_tags(self._enabled_tags)
        if not self.scheduler.scan_classes:
            log.warning("device has no enabled tags; worker idle", extra={"device_id": self.device.device_id})
        while not self._stop.is_set():
            try:
                if not await self.connection.ensure_connected(self._stop):
                    continue
                due, wait = self.scheduler.next_due()
                if not due:
                    await sleep_or_stop(self._stop, wait)
                    continue
                for sc in due:
                    if self._stop.is_set() or not await self._poll(sc):
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("device worker loop error; continuing", extra={"device_id": self.device.device_id})
                await sleep_or_stop(self._stop, 1.0)
        await self.connection.close()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ on-demand I/O (write manager)
    async def write(self, tag: TagConfig, value: TagValue, timeout_s: float) -> WriteResult:
        if not self.driver.is_connected():
            raise DriverConnectionError(f"{self.device.device_id}: not connected")
        async with self.io_lock:
            return await asyncio.wait_for(self.driver.write_tag(tag, value), timeout_s)

    async def read_now(self, tag: TagConfig, timeout_s: float) -> RawReadResult:
        if not self.driver.is_connected():
            raise DriverConnectionError(f"{self.device.device_id}: not connected")
        async with self.io_lock:
            return await asyncio.wait_for(self.driver.read_tag(tag), timeout_s)
