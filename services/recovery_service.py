"""Database maintenance: partitions ahead, retention (drop/archive), table cleanup.

Runs once shortly after the database becomes available and then daily at
``retention.maintenance_hour_utc``. Each action is audited.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from database.mysql_client import MySQLClient, MySQLParams
from database.repository import Repository
from models.config import GatewayConfig
from utils.timeutil import utc_now

log = logging.getLogger(__name__)


class RecoveryService:
    def __init__(self, config: GatewayConfig, repository: Repository) -> None:
        self.config = config
        self.repo = repository
        self.last_run: str | None = None
        self.last_error: str | None = None

    async def ensure_partitions(self) -> None:
        r = self.config.settings.retention
        today = utc_now().date()
        created = await self.repo.ensure_partitions("plc_tag_history", start=today - timedelta(days=1),
                                                    until=today + timedelta(days=r.partitions_ahead_days),
                                                    monthly=False)
        created += await self.repo.ensure_partitions("plc_events", start=today.replace(day=1),
                                                     until=today + timedelta(days=62), monthly=True)
        if created:
            log.info("created partitions: %s", ", ".join(created))

    async def apply_retention(self) -> None:
        r = self.config.settings.retention
        today = utc_now().date()
        for table, days in (("plc_tag_history", r.history_days), ("plc_events", r.events_days)):
            cutoff: date = today - timedelta(days=days)
            for name in await self.repo.expired_partitions(table, cutoff):
                if r.archive_mode == "csv_gzip":
                    path = await self.repo.export_partition(table, name, self.config.resolve_path(r.archive_dir))
                    log.info("archived %s.%s to %s", table, name, path)
                await self.repo.drop_partition(table, name)
                await self.repo.insert_audit(action="RETENTION_DROP_PARTITION", principal="system", result="OK",
                                             detail={"table": table, "partition": name, "archive": r.archive_mode})
                log.info("dropped expired partition %s.%s", table, name)
        now = utc_now()
        n = await self.repo.delete_older_than("gateway_health", "snapshot_ts", now - timedelta(days=r.health_days))
        if n:
            log.info("deleted %d old health rows", n)
        if MySQLParams.maintenance_available(self.config.settings.database):
            maint = MySQLClient(MySQLParams.from_settings(self.config.settings.database, maintenance=True))
            await maint.start()
            try:
                n = await Repository(maint, self.repo.gateway_id).delete_older_than(
                    "audit_logs", "ts", now - timedelta(days=r.audit_days))
                if n:
                    log.info("deleted %d audit rows older than %d days", n, r.audit_days)
            finally:
                await maint.close()

    async def run_once(self) -> None:
        try:
            await self.ensure_partitions()
            await self.apply_retention()
            self.last_run = utc_now().isoformat()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.error("database maintenance failed: %s", exc)

    async def run(self, stop: asyncio.Event, db_ready: asyncio.Event) -> None:
        while not stop.is_set():
            ready_wait = asyncio.create_task(db_ready.wait())
            stop_wait = asyncio.create_task(stop.wait())
            await asyncio.wait({ready_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            ready_wait.cancel()
            stop_wait.cancel()
            if stop.is_set():
                return
            await self.run_once()
            now = utc_now()
            hour = self.config.settings.retention.maintenance_hour_utc
            nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            # partitions must never run out even if the daily slot is missed: cap the wait at 6 h
            wait = min((nxt - now).total_seconds(), 6 * 3600)
            try:
                await asyncio.wait_for(stop.wait(), wait)
            except TimeoutError:
                continue
