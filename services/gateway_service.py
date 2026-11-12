"""Composition root and lifecycle of the gateway (dependency injection happens here)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.device_worker import DeviceWorker
from core.driver_manager import DriverManager
from core.tag_manager import TagManager
from core.write_manager import WriteManager
from database.batch_writer import BatchWriter
from database.mysql_client import MySQLClient, MySQLParams
from database.repository import Repository
from integrations.base import IntegrationDispatcher, build_adapters
from models.config import GatewayConfig
from models.events import TagEvent
from models.reading import TagReading
from services.buffer_service import BufferService
from services.health_service import HealthService
from services.metrics import Metrics
from services.recovery_service import RecoveryService
from utils.backoff import Backoff
from utils.exceptions import DatabaseUnavailableError

log = logging.getLogger(__name__)


class _NullAuditStore:
    """Used when the database is disabled: every write is rejected (fail closed)."""

    async def get_write_command(self, request_id: str) -> None:
        raise DatabaseUnavailableError("database disabled; writes require the audit store")

    async def insert_write_command(self, cmd: Any) -> None:
        raise DatabaseUnavailableError("database disabled; writes require the audit store")

    async def update_write_command(self, cmd: Any) -> None:
        raise DatabaseUnavailableError("database disabled")

    async def insert_audit(self, **kwargs: Any) -> None:
        log.info("audit (no database): %s", kwargs)


class _Sink:
    def __init__(self, buffer: BufferService | None, dispatcher: IntegrationDispatcher | None,
                 metrics: Metrics) -> None:
        self.buffer = buffer
        self.dispatcher = dispatcher
        self.metrics = metrics

    def submit(self, readings: list[TagReading], events: list[TagEvent]) -> None:
        if self.buffer is not None:
            self.buffer.submit(readings, events)
        else:
            self.metrics.inc("records_not_persisted_total", len(readings) + len(events))
        if self.dispatcher is not None:
            self.dispatcher.submit(readings, events)


class GatewayService:
    def __init__(self, config: GatewayConfig, *, metrics: Metrics | None = None) -> None:
        self.config = config
        self.metrics = metrics or Metrics()
        self.tag_manager = TagManager(config)
        self.driver_manager = DriverManager()
        self.workers: dict[str, DeviceWorker] = {}
        self.buffer: BufferService | None = None
        self.mysql: MySQLClient | None = None
        self.repository: Repository | None = None
        self.batch_writer: BatchWriter | None = None
        self.recovery: RecoveryService | None = None
        self.dispatcher: IntegrationDispatcher | None = None
        self.write_manager: WriteManager | None = None
        self.health = HealthService(self)
        self.stop_event = asyncio.Event()
        self.db_ready = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self.started = False

    # ------------------------------------------------------------------ database readiness
    async def _ensure_db_ready(self) -> None:
        assert self.mysql is not None and self.repository is not None
        await self.mysql.start()
        await self.repository.sync_registry(self.config)
        self.db_ready.set()

    async def start(self) -> None:
        s = self.config.settings
        gid = s.gateway.gateway_id
        log.info("starting gateway %s (%s)", gid, s.gateway.environment)
        if s.gateway.read_only_mode:
            log.info("read_only_mode is ON: all PLC writes are rejected")

        adapters = build_adapters(s.integrations, self.config.resolve_path)
        if adapters:
            self.dispatcher = IntegrationDispatcher(adapters, self.metrics)
            await self.dispatcher.start()

        if s.database.enabled:
            self.buffer = BufferService(s.buffer, self.config.resolve_path(s.buffer.spool_path), self.metrics)
            await self.buffer.start()
            self.mysql = MySQLClient(MySQLParams.from_settings(s.database), pool_min=s.database.pool_min,
                                     pool_max=s.database.pool_max)
            self.repository = Repository(self.mysql, gid)
            r = s.database.retry
            self.batch_writer = BatchWriter(self.buffer, self.repository, self.metrics,
                                            batch_size=s.buffer.batch_size,
                                            flush_interval_s=s.buffer.flush_interval_ms / 1000,
                                            backoff=Backoff(r.initial_delay_s, r.max_delay_s, r.jitter),
                                            ensure_ready=self._ensure_db_ready)
            self.recovery = RecoveryService(self.config, self.repository)
            self._tasks.append(asyncio.create_task(self.batch_writer.run(self.stop_event), name="batch-writer"))
            self._tasks.append(asyncio.create_task(self.recovery.run(self.stop_event, self.db_ready),
                                                   name="db-maintenance"))
        else:
            log.warning("database.enabled is false: readings are NOT persisted (latest values and integrations only)")

        sink = _Sink(self.buffer, self.dispatcher, self.metrics)
        for device in self.config.devices(enabled_only=True):
            driver = self.driver_manager.create(device)
            worker = DeviceWorker(device, driver, self.tag_manager, sink, self.metrics)
            self.workers[device.device_id] = worker
            self._tasks.append(asyncio.create_task(worker.run(), name=f"device-{device.device_id}"))

        store: Any = self.repository if self.repository is not None else _NullAuditStore()
        self.write_manager = WriteManager(self.config, self.tag_manager, store, dict(self.workers))
        self._tasks.append(asyncio.create_task(self.health.run(self.stop_event), name="health"))
        self.started = True
        if self.repository is not None:
            asyncio.create_task(self._audit_start())  # noqa: RUF006 - fire and forget, failures logged
        log.info("gateway started: %d device worker(s)", len(self.workers))

    async def _audit_start(self) -> None:
        try:
            await asyncio.wait_for(self.db_ready.wait(), 60)
            assert self.repository is not None
            from core.config_loader import config_fingerprint

            await self.repository.insert_audit(action="GATEWAY_START", principal="system", result="OK",
                                               detail={"config_sha256": config_fingerprint(self.config.config_dir),
                                                       "devices": list(self.workers)})
        except Exception as exc:
            log.debug("start audit not recorded: %s", exc)

    async def stop(self) -> None:
        if not self.started:
            return
        log.info("stopping gateway")
        for worker in self.workers.values():
            worker.stop()
        self.stop_event.set()
        done, pending = await asyncio.wait(self._tasks, timeout=30)
        for task in pending:
            log.warning("task %s did not stop in time; cancelling", task.get_name())
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception():
                log.error("task %s ended with error: %r", task.get_name(), task.exception())
        await self.driver_manager.shutdown()
        if self.dispatcher:
            await self.dispatcher.stop()
        if self.repository is not None and self.db_ready.is_set():
            try:
                await asyncio.wait_for(self.repository.insert_audit(
                    action="GATEWAY_STOP", principal="system", result="OK"), 5)
            except Exception as exc:
                log.debug("stop audit not recorded: %s", exc)
        if self.buffer:
            await self.buffer.stop()
        if self.mysql:
            await self.mysql.close()
        self.started = False
        log.info("gateway stopped")
