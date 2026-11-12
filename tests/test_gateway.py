"""End-to-end: GatewayService + simulators (+ MySQL when MYSQL_TEST_DSN is set). Marker: sim."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from core.config_loader import load_config
from models.events import EventType
from models.reading import Quality
from services.gateway_service import GatewayService
from simulators.mc_sim import MCSimulator
from simulators.modbus_sim import ModbusSimulator

pytestmark = pytest.mark.sim
ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("MYSQL_TEST_DSN")


def build_config(tmp: Path, modbus_port: int, mc_port: int, *, database: bool):
    cfg = tmp / "config"
    shutil.copytree(ROOT / "config" / "sim", cfg)
    plants = yaml.safe_load((cfg / "plants.yaml").read_text())
    devices = plants["plants"][0]["devices"]
    plants["plants"][0]["devices"] = [d for d in devices if d["device_id"] in ("modbus_sim", "mc_sim")]
    for d in plants["plants"][0]["devices"]:
        d["connection"]["port"] = modbus_port if d["device_id"] == "modbus_sim" else mc_port
        d["reconnect"] = {"initial_delay_s": 0.2, "max_delay_s": 0.5, "jitter": 0}
        d["circuit_breaker"] = {"failure_threshold": 100, "open_duration_s": 1}
    (cfg / "plants.yaml").write_text(yaml.safe_dump(plants))
    settings = yaml.safe_load((cfg / "settings.yaml").read_text())
    settings["database"]["enabled"] = database
    settings["buffer"]["flush_interval_ms"] = 200
    settings["health"]["snapshot_interval_s"] = 5
    for key in ("status_file", "write_kill_switch_file"):
        settings["gateway"][key] = str(tmp / Path(settings["gateway"][key]).name)
    settings["buffer"]["spool_path"] = str(tmp / "spool.sqlite3")
    (cfg / "settings.yaml").write_text(yaml.safe_dump(settings))
    for name in ("tags_modbus.yaml", "tags_mc.yaml"):
        data = yaml.safe_load((cfg / name).read_text())
        for t in data["tags"]:
            t["poll_interval_ms"] = 200
        (cfg / name).write_text(yaml.safe_dump(data))
    return load_config(cfg)


def set_db_env(monkeypatch):
    u = urlparse(DSN)
    monkeypatch.setenv("MYSQL_HOST", u.hostname)
    monkeypatch.setenv("MYSQL_PORT", str(u.port or 3306))
    monkeypatch.setenv("MYSQL_DATABASE", u.path.lstrip("/"))
    monkeypatch.setenv("MYSQL_USER", u.username)
    monkeypatch.setenv("MYSQL_PASSWORD", u.password)


async def wait_for(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.fixture
async def sims():
    mb = ModbusSimulator(port=0, units=frozenset({1}))
    mcs = MCSimulator(port=0)
    await mb.start()
    await mcs.start()
    mb.set_float(mb.holding, 0, 231.0)
    mcs.words["D"][100] = 1500
    yield mb, mcs
    await mb.stop()
    await mcs.stop()


async def test_gateway_without_database_polls_and_detects_comm_loss(tmp_path, sims):
    mb, mcs = sims
    config = build_config(tmp_path, mb.port, mcs.port, database=False)
    gw = GatewayService(config)
    events = []
    await gw.start()
    try:
        tm = gw.tag_manager
        assert await wait_for(lambda: (r := tm.latest_one("modbus_sim", "voltage_l1")) is not None
                              and r.quality is Quality.GOOD)
        assert tm.latest_one("modbus_sim", "voltage_l1").value == 231.0
        assert await wait_for(lambda: (r := tm.latest_one("mc_sim", "speed")) is not None and r.value == 1500)

        # capture events through the sink by wrapping submit
        worker = gw.workers["modbus_sim"]
        original = worker.sink.submit
        worker.sink.submit = lambda r, e: (events.extend(e), original(r, e))
        port = mb.port
        await mb.stop()                                       # PLC goes away
        assert await wait_for(lambda: any(e.event_type is EventType.COMM_LOST for e in events))
        assert await wait_for(lambda: tm.latest_one("modbus_sim", "voltage_l1").quality is Quality.BAD_NOT_CONNECTED)
        assert tm.latest_one("mc_sim", "speed").quality is Quality.GOOD     # other device unaffected
        assert sum(e.event_type is EventType.COMM_LOST for e in events) == 1  # no event storm

        mb.port = port
        await mb.start()                                      # PLC back
        assert await wait_for(lambda: any(e.event_type is EventType.COMM_RESTORED for e in events), 15)
        assert await wait_for(lambda: tm.latest_one("modbus_sim", "voltage_l1").quality is Quality.GOOD)

        gw.health.write_status_file()
        status = json.loads(config.resolve_path(config.settings.gateway.status_file).read_text())
        assert status["devices_enabled"] == 2
        assert {d["device_id"] for d in status["devices"]} == {"modbus_sim", "mc_sim"}
    finally:
        await gw.stop()


@pytest.mark.mysql
@pytest.mark.skipif(not DSN, reason="MYSQL_TEST_DSN not set")
async def test_gateway_end_to_end_mysql_with_outage_replay(tmp_path, sims, monkeypatch):
    from database.mysql_client import MySQLClient, MySQLParams
    from scripts.init_database import create_partitions, migrate

    set_db_env(monkeypatch)
    db = MySQLClient(MySQLParams.from_dsn(DSN))
    await db.start()
    async with db.transaction() as cur:
        await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        await cur.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                          "WHERE TABLE_SCHEMA = DATABASE()")
        for name, kind in await cur.fetchall():
            await cur.execute(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} IF EXISTS `{name}`")
    await migrate(db, verbose=False)
    await create_partitions(db, 2, "gw_dev_sim")

    mb, mcs = sims
    config = build_config(tmp_path, mb.port, mcs.port, database=True)
    gw = GatewayService(config)
    await gw.start()
    try:
        async def count(table):
            return (await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}"))["n"]

        assert await wait_for(lambda: gw.batch_writer.db_ok, 15)
        await asyncio.sleep(1.5)
        assert await count("plc_tag_history") > 0
        latest = await db.fetch_all("SELECT * FROM v_tag_latest WHERE device_id = 'mc_sim' AND tag_id = 'speed'")
        assert latest and latest[0]["value_num"] == 1500

        # database outage: writes fail, data accumulates in the spool, then replays
        # outage: kill the gateway's pool under it; the writer must detect it, keep data in the
        # spool, rebuild the pool itself and replay (no test intervention to recover)
        before = await count("plc_tag_history")
        failures_before = gw.metrics.counter("db_write_failures_total")
        await gw.mysql.close()
        assert await wait_for(lambda: gw.metrics.counter("db_write_failures_total") > failures_before, 10)
        grew = False
        for _ in range(100):
            if gw.batch_writer.db_ok and await count("plc_tag_history") > before + 10:
                grew = True
                break
            await asyncio.sleep(0.2)
        assert grew, "writer did not recover from pool loss"
        assert gw.metrics.counter_total("records_dropped_total") == 0
    finally:
        await gw.stop()
        audit = await db.fetch_all("SELECT action FROM audit_logs ORDER BY audit_pk")
        await db.close()
    assert [a["action"] for a in audit][:1] == ["GATEWAY_START"]
