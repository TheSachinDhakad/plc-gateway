"""Gateway/device health: live status for the API and periodic snapshots to MySQL."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import psutil

from models.plc import DeviceStatus
from utils.timeutil import to_iso, to_mysql, utc_now

if TYPE_CHECKING:
    from services.gateway_service import GatewayService

log = logging.getLogger(__name__)


class HealthService:
    def __init__(self, gateway: GatewayService) -> None:
        self.gw = gateway
        self._proc = psutil.Process()
        self._proc.cpu_percent(None)   # prime the CPU counter
        self._last_db_snapshot = 0.0

    def device_status(self) -> list[dict[str, Any]]:
        m = self.gw.metrics
        out = []
        for device in self.gw.tag_manager.devices():
            worker = self.gw.workers.get(device.device_id)
            if worker is None:
                status = DeviceStatus.DISABLED
                cm = None
            else:
                cm = worker.connection
                status = cm.status
            did = device.device_id
            out.append({
                "plant_id": device.plant_id,
                "device_id": did,
                "name": device.name,
                "driver": str(device.driver),
                "enabled": worker is not None,
                "status": status.value,
                "connected": status is DeviceStatus.CONNECTED,
                "write_enabled": device.write_enabled,
                "circuit_state": cm.breaker.state.value if cm else None,
                "last_error": cm.last_error if cm else None,
                "last_connected_at": to_iso(cm.last_connected_at) if cm else None,
                "last_poll_at": to_iso(worker.last_poll_at) if worker else None,
                "reads_ok": m.counter("reads_ok_total", device=did),
                "reads_failed": m.counter("reads_failed_total", device=did),
                "reconnects": m.counter("reconnects_total", device=did),
                "scan_overruns": m.counter("scan_overruns_total", device=did),
                "poll_duration_ms": m.duration("poll_duration_ms", device=did),
                "scan_classes": [{"interval_ms": sc.interval_ms, "tags": len(sc.tags), "cycles": sc.cycles,
                                  "overruns": sc.overruns, "last_duration_ms": round(sc.last_duration_ms, 2)}
                                 for sc in worker.scheduler.scan_classes] if worker else [],
            })
        return out

    def process_stats(self) -> dict[str, Any]:
        with self._proc.oneshot():
            return {"cpu_percent": self._proc.cpu_percent(None),
                    "rss_mb": round(self._proc.memory_info().rss / 1024 / 1024, 1),
                    "threads": self._proc.num_threads()}

    def status(self) -> dict[str, Any]:
        m = self.gw.metrics
        buffer = self.gw.buffer
        writer = self.gw.batch_writer
        devices = self.device_status()
        enabled = [d for d in devices if d["enabled"]]
        connected = sum(1 for d in enabled if d["connected"])
        db_ok = bool(writer and writer.db_ok)
        dropped = m.counter_total("records_dropped_total")
        overall = "ok"
        if connected < len(enabled) or (self.gw.config.settings.database.enabled and not db_ok):
            overall = "degraded"
        if enabled and connected == 0:
            overall = "critical"
        writes_enabled, reason = self.gw.write_manager.writes_globally_enabled() if self.gw.write_manager else (
            False, "NOT_STARTED")
        return {
            "gateway_id": self.gw.config.settings.gateway.gateway_id,
            "status": overall,
            "timestamp": to_iso(utc_now()),
            "uptime_s": m.snapshot()["uptime_s"],
            "devices_enabled": len(enabled),
            "devices_connected": connected,
            "database": {"enabled": self.gw.config.settings.database.enabled, "available": db_ok,
                         "last_error": writer.last_error if writer else None,
                         "insert_duration_ms": m.duration("db_insert_duration_ms")},
            "buffer": {"queue_size": buffer.queue_size if buffer else 0,
                       "spool_rows": buffer.spool_rows if buffer else 0,
                       "durability_mode": self.gw.config.settings.buffer.durability_mode,
                       "records_written_total": m.counter("records_written_total"),
                       "records_dropped_total": dropped},
            "writes": {"enabled": writes_enabled, "disabled_reason": reason},
            "process": self.process_stats(),
            "integrations": self.gw.dispatcher.health() if self.gw.dispatcher else [],
            "devices": devices,
        }

    def snapshot_rows(self) -> list[dict[str, Any]]:
        now = to_mysql(utc_now())
        gid = self.gw.config.settings.gateway.gateway_id
        status = self.status()
        rows = [{
            "snapshot_ts": now, "gateway_id": gid, "device_id": "_gateway", "status": status["status"],
            "connected": None, "queue_size": status["buffer"]["queue_size"],
            "spool_rows": status["buffer"]["spool_rows"], "dropped_total": status["buffer"]["records_dropped_total"],
            "db_insert_p95_ms": status["database"]["insert_duration_ms"]["p95"],
            "cpu_percent": status["process"]["cpu_percent"], "rss_mb": status["process"]["rss_mb"],
            "detail": {"records_written_total": status["buffer"]["records_written_total"]},
        }]
        for d in status["devices"]:
            if not d["enabled"]:
                continue
            rows.append({
                "snapshot_ts": now, "gateway_id": gid, "device_id": d["device_id"], "status": d["status"],
                "connected": int(d["connected"]), "reads_ok": d["reads_ok"], "reads_failed": d["reads_failed"],
                "reconnects": d["reconnects"], "poll_p95_ms": d["poll_duration_ms"]["p95"],
                "detail": {"last_error": d["last_error"], "scan_overruns": d["scan_overruns"]},
            })
        return rows

    def write_status_file(self) -> None:
        """Atomically write the live status JSON (local inspection without any network API)."""
        path = self.gw.config.resolve_path(self.gw.config.settings.gateway.status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {**self.status(), "metrics": self.gw.metrics.snapshot()}
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.gw.config.settings.health.snapshot_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), min(interval, 10))
                break
            except TimeoutError:
                pass
            try:
                self.write_status_file()
            except Exception as exc:
                log.warning("status file not written: %s", exc)
            now = asyncio.get_running_loop().time()
            if now - self._last_db_snapshot < interval:
                continue
            self._last_db_snapshot = now
            repo = self.gw.repository
            if repo is None or not (self.gw.batch_writer and self.gw.batch_writer.db_ok):
                continue
            try:
                await repo.insert_health(self.snapshot_rows())
            except Exception as exc:
                log.debug("health snapshot not stored: %s", exc)
