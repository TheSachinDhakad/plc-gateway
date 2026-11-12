"""Integration boundary.

Adapters receive normalized :class:`TagReading`/:class:`TagEvent` objects only — they never
see drivers, so PLC protocols stay decoupled from Grafana, central servers or SAP.
Each adapter has its own bounded outbox; a slow adapter cannot block acquisition or MySQL.

SAP: no SAP interface is assumed. A SAP adapter implements :class:`IntegrationAdapter`
once the SAP interface specification (IDoc/OData/RFC/PI-PO endpoint) is provided.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from models.events import TagEvent
from models.reading import TagReading
from services.metrics import Metrics

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AdapterHealth:
    name: str
    type: str
    ok: bool
    queued: int = 0
    delivered: int = 0
    failed: int = 0
    dropped: int = 0
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class IntegrationAdapter(abc.ABC):
    name: str
    type: str
    publish_readings_enabled: bool = True
    publish_events_enabled: bool = True

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def publish_readings(self, readings: Sequence[TagReading]) -> None:
        """Accept readings for delivery. Must not block for network I/O."""

    @abc.abstractmethod
    async def publish_events(self, events: Sequence[TagEvent]) -> None: ...

    @abc.abstractmethod
    def health(self) -> AdapterHealth: ...


class IntegrationDispatcher:
    """Fan out pipeline output to adapters through per-adapter bounded queues."""

    def __init__(self, adapters: list[IntegrationAdapter], metrics: Metrics, queue_size: int = 10_000) -> None:
        self.adapters = adapters
        self.metrics = metrics
        self._queues: dict[str, asyncio.Queue[tuple[list[TagReading], list[TagEvent]]]] = {
            a.name: asyncio.Queue(maxsize=queue_size) for a in adapters}
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for adapter in self.adapters:
            await adapter.start()
            self._tasks.append(asyncio.create_task(self._pump(adapter), name=f"integration-{adapter.name}"))

    def submit(self, readings: list[TagReading], events: list[TagEvent]) -> None:
        for adapter in self.adapters:
            r = readings if adapter.publish_readings_enabled else []
            e = events if adapter.publish_events_enabled else []
            if not r and not e:
                continue
            try:
                self._queues[adapter.name].put_nowait((r, e))
            except asyncio.QueueFull:
                self.metrics.inc("integration_dropped_total", len(r) + len(e), adapter=adapter.name)
                log.warning("integration %s queue full; batch dropped", adapter.name)

    async def _pump(self, adapter: IntegrationAdapter) -> None:
        queue = self._queues[adapter.name]
        while True:
            readings, events = await queue.get()
            try:
                if readings:
                    await adapter.publish_readings(readings)
                if events:
                    await adapter.publish_events(events)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("integration %s failed to accept batch", adapter.name)
                self.metrics.inc("integration_errors_total", adapter=adapter.name)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:
                log.exception("integration %s stop failed", adapter.name)

    def health(self) -> list[dict[str, Any]]:
        out = []
        for a in self.adapters:
            h = a.health()
            out.append({"name": h.name, "type": h.type, "ok": h.ok, "queued": h.queued + self._queues[a.name].qsize(),
                        "delivered": h.delivered, "failed": h.failed, "dropped": h.dropped,
                        "last_error": h.last_error, **h.extra})
        return out


def build_adapters(configs: list[Any], resolve_path: Any) -> list[IntegrationAdapter]:
    adapters: list[IntegrationAdapter] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        if cfg.type == "rest_forwarder":
            from integrations.rest_forwarder import RestForwarder

            adapters.append(RestForwarder(cfg, resolve_path(cfg.outbox_path),
                                          resolve_path(cfg.ca_file) if cfg.ca_file else None))
        elif cfg.type == "mqtt_publisher":
            from integrations.mqtt_publisher import MqttPublisher

            adapters.append(MqttPublisher(cfg, resolve_path(cfg.ca_file) if cfg.ca_file else None))
    return adapters
