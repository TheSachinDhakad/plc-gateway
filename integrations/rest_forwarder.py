"""Forward readings/events to a central server over HTTPS with a durable SQLite outbox.

Payload (JSON POST to ``url``)::

    {"gateway_id": "...", "sent_at": "ISO", "readings": [TagReading.to_dict()...], "events": [...]}

Delivery is at-least-once: the receiver must deduplicate on ``reading_uid`` / ``event_uid``.
HTTP 2xx = delivered; 4xx (except 408/429) = permanent rejection (logged, counted, dropped);
everything else is retried with backoff. Authentication: ``Authorization: Bearer <token_env>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from integrations.base import AdapterHealth, IntegrationAdapter
from models.config import RestForwarderConfig
from models.events import TagEvent
from models.reading import TagReading
from services.buffer_service import KIND_EVENT, KIND_READING, SpoolStore
from utils.backoff import Backoff
from utils.timeutil import to_iso, utc_now

log = logging.getLogger(__name__)


class RestForwarder(IntegrationAdapter):
    type = "rest_forwarder"

    def __init__(self, cfg: RestForwarderConfig, outbox_path: Path, ca_file: Path | None) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.publish_readings_enabled = "readings" in cfg.publish
        self.publish_events_enabled = "events" in cfg.publish
        self.outbox = SpoolStore(outbox_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"outbox-{cfg.name}")
        self._ca_file = ca_file
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._backoff = Backoff(cfg.retry.initial_delay_s, cfg.retry.max_delay_s, cfg.retry.jitter)
        self._queued = 0
        self.delivered = self.failed = self.dropped = 0
        self.last_error: str | None = None
        self.gateway_id = socket.gethostname()

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def start(self) -> None:
        self._queued = await self._run(self.outbox.open)
        verify: Any = self.cfg.verify_tls
        if verify and self._ca_file:
            verify = str(self._ca_file)
        headers = {"Content-Type": "application/json", "User-Agent": "industrial-plc-gateway"}
        if self.cfg.token_env:
            token = os.environ.get(self.cfg.token_env)
            if not token:
                raise RuntimeError(f"integration {self.name}: env {self.cfg.token_env} not set")
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(timeout=self.cfg.timeout_s, verify=verify, headers=headers)
        self._task = asyncio.create_task(self._deliver_loop(), name=f"rest-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        await self._run(self.outbox.close)
        self._executor.shutdown(wait=True)

    async def _enqueue(self, items: list[tuple[int, dict[str, Any]]]) -> None:
        await self._run(self.outbox.append, items)
        self._queued += len(items)
        if self._queued > self.cfg.outbox_max_rows:
            dropped = await self._run(self.outbox.enforce_limit, self.cfg.outbox_max_rows, "drop_cyclic_keep_events")
            n = sum(dropped.values())
            self.dropped += n
            self._queued -= n
            log.error("integration %s outbox full: dropped %d oldest records", self.name, n)
        self._wake.set()

    async def publish_readings(self, readings: Sequence[TagReading]) -> None:
        await self._enqueue([(KIND_READING, r.to_dict()) for r in readings])

    async def publish_events(self, events: Sequence[TagEvent]) -> None:
        await self._enqueue([(KIND_EVENT, e.to_dict()) for e in events])

    async def _deliver_loop(self) -> None:
        assert self._client is not None
        while not self._stop.is_set():
            rows = await self._run(self.outbox.fetch, self.cfg.batch_size)
            if not rows:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 5)
                continue
            body = {"gateway_id": self.gateway_id, "sent_at": to_iso(utc_now()),
                    "readings": [p for _, k, p in rows if k == KIND_READING],
                    "events": [p for _, k, p in rows if k == KIND_EVENT]}
            ids = [i for i, _, _ in rows]
            try:
                resp = await self._client.post(self.cfg.url, content=json.dumps(body))
                status = resp.status_code
            except httpx.HTTPError as exc:
                status = None
                self.last_error = f"{type(exc).__name__}: {exc}"
            if status is not None and 200 <= status < 300:
                await self._run(self.outbox.delete, ids)
                self._queued = max(0, self._queued - len(ids))
                self.delivered += len(ids)
                self.last_error = None
                self._backoff.reset()
                continue
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                await self._run(self.outbox.delete, ids)
                self._queued = max(0, self._queued - len(ids))
                self.dropped += len(ids)
                self.last_error = f"HTTP {status} (permanent): batch discarded"
                log.error("integration %s: central server rejected batch with HTTP %s", self.name, status)
                continue
            if status is not None:
                self.last_error = f"HTTP {status}"
            self.failed += 1
            delay = self._backoff.next_delay()
            log.warning("integration %s delivery failed (%s); retry in %.1fs", self.name, self.last_error, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay)

    def health(self) -> AdapterHealth:
        return AdapterHealth(self.name, self.type, ok=self.last_error is None, queued=self._queued,
                             delivered=self.delivered, failed=self.failed, dropped=self.dropped,
                             last_error=self.last_error, extra={"url": self.cfg.url})
