"""MySQL integration tests (marker: mysql). Require MYSQL_TEST_DSN pointing at a disposable
database, e.g. mysql://gw_test:pw@127.0.0.1:3306/plc_gateway_test — ALL TABLES ARE DROPPED."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from core.config_loader import load_config
from core.data_normalizer import DataNormalizer
from core.write_manager import WriteCommand
from database.mysql_client import MySQLClient, MySQLParams
from database.repository import Repository
from models.events import EventType, TagEvent
from models.reading import Quality, RawReadResult, WriteStatus
from scripts.init_database import create_partitions, migrate
from utils.exceptions import DatabaseError
from utils.timeutil import utc_now

DSN = os.environ.get("MYSQL_TEST_DSN")
pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not DSN, reason="MYSQL_TEST_DSN not set")]
ROOT = Path(__file__).resolve().parent.parent


async def reset(db: MySQLClient) -> None:
    await db.execute("SET FOREIGN_KEY_CHECKS = 0")
    for row in await db.fetch_all("SELECT TABLE_NAME AS t, TABLE_TYPE AS k FROM information_schema.TABLES "
                                  "WHERE TABLE_SCHEMA = DATABASE()"):
        kind = "VIEW" if row["k"] == "VIEW" else "TABLE"
        await db.execute(f"DROP {kind} IF EXISTS `{row['t']}`")


@pytest.fixture
async def repo():
    db = MySQLClient(MySQLParams.from_dsn(DSN), pool_max=3)
    await db.start()
    async with db.transaction() as cur:
        await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        await cur.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                          "WHERE TABLE_SCHEMA = DATABASE()")
        for name, kind in await cur.fetchall():
            await cur.execute(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} IF EXISTS `{name}`")
    await migrate(db, verbose=False)
    await create_partitions(db, 3, "gw_test")
    repository = Repository(db, "gw_test")
    config = load_config(ROOT / "config" / "sim")
    await repository.sync_registry(config)
    repository.config = config
    yield repository
    await db.close()


def readings(repo, values, quality=Quality.GOOD, ts=None):
    cfg = repo.config
    device = cfg.device("s7_sim")
    tag = next(t for t in cfg.tags["s7_sim"] if t.tag_id == "temperature")
    out = []
    for i, v in enumerate(values):
        t = (ts or utc_now()) + timedelta(milliseconds=i)
        out.append(DataNormalizer().normalize(device, tag, RawReadResult(tag.tag_id, v, quality, t), t))
    return out


async def test_migrations_idempotent_and_partitions(repo):
    assert await migrate(repo.db, verbose=False) == []
    names = [p["name"] for p in await repo.list_partitions("plc_tag_history")]
    today = utc_now().date()
    assert f"p{today:%Y%m%d}" in names and names[-1] == "p_future"
    assert await create_partitions(repo.db, 3, "gw_test") == []


async def test_registry_sync(repo):
    assert len(repo.device_pks) == 4
    assert ("s7_sim", "temperature") in repo.tag_pks
    await repo.sync_registry(repo.config)          # idempotent
    assert len(repo.tag_pks) == 14


async def test_write_batch_history_latest_events_and_idempotent_replay(repo):
    batch = readings(repo, [20.0, 21.5, float("nan")])
    event = TagEvent("plant_sim", "s7_sim", "temperature", EventType.THRESHOLD_HIGH, utc_now(), None, 99.0,
                     threshold=27.5)
    h, e, skipped = await repo.write_batch(batch, [event])
    assert (h, e, skipped) == (3, 1, 0)
    # replay of the same batch (crash between commit and spool delete) creates no duplicates
    await repo.write_batch(batch, [event])
    rows = await repo.db.fetch_all("SELECT COUNT(*) AS n FROM plc_tag_history")
    assert rows[0]["n"] == 3
    assert (await repo.db.fetch_one("SELECT COUNT(*) AS n FROM plc_events"))["n"] == 1
    latest = await repo.latest(device_id="s7_sim", tag_id="temperature")
    assert latest[0]["value"] == "nan" and latest[0]["value_type"] == "float"
    hist, more = await repo.history(start=utc_now() - timedelta(minutes=1), end=utc_now() + timedelta(minutes=1),
                                    device_id="s7_sim", tag_id="temperature", limit=2, offset=0)
    assert [r["value"] for r in hist] == [20.0, 21.5] and more is True
    events, _ = await repo.events(start=utc_now() - timedelta(minutes=1), end=utc_now() + timedelta(minutes=1),
                                  device_id=None, tag_id=None, event_type="THRESHOLD_HIGH", limit=10, offset=0)
    assert events[0]["new_value"] == 99.0


async def test_latest_does_not_regress_on_old_replay(repo):
    new = readings(repo, [30.0])
    old = readings(repo, [10.0], ts=utc_now() - timedelta(hours=1))
    await repo.write_batch(new, [])
    await repo.write_batch(old, [])
    latest = await repo.latest(device_id="s7_sim", tag_id="temperature")
    assert latest[0]["value"] == 30.0


async def test_typed_columns(repo):
    cfg = repo.config
    device = cfg.device("modbus_sim")
    norm = DataNormalizer()
    now = utc_now()
    tags = {t.tag_id: t for t in cfg.tags["modbus_sim"]}
    rs = [norm.normalize(device, tags["breaker_closed"], RawReadResult("breaker_closed", True, Quality.GOOD, now)),
          norm.normalize(device, tags["energy_total"], RawReadResult("energy_total", 4_000_000_000, Quality.GOOD, now)),
          norm.normalize(device, tags["voltage_l1"], RawReadResult("voltage_l1", None, Quality.BAD_TIMEOUT, now))]
    await repo.write_batch(rs, [])
    latest = {r["tag_id"]: r for r in await repo.latest(device_id="modbus_sim")}
    assert latest["breaker_closed"]["value"] is True
    assert latest["energy_total"]["value"] == 4_000_000_000
    assert latest["voltage_l1"]["value"] is None and latest["voltage_l1"]["quality"] == "BAD_TIMEOUT"


async def test_transaction_rollback(repo):
    with pytest.raises(DatabaseError):
        async with repo.db.transaction() as cur:
            await cur.execute("INSERT INTO audit_logs (ts, gateway_id, action, result) VALUES (NOW(6), 'g', 'X', 'OK')")
            await cur.execute("INSERT INTO no_such_table VALUES (1)")
    assert (await repo.db.fetch_one("SELECT COUNT(*) AS n FROM audit_logs"))["n"] == 0


async def test_write_command_roundtrip_and_unique_request_id(repo):
    cmd = WriteCommand("req-db-0001", "plant_sim", "s7_sim", "test_write_value", "alice", 12.5, "test")
    await repo.insert_write_command(cmd)
    cmd.status, cmd.readback_value, cmd.completed_at = WriteStatus.SUCCESS, 12.5, utc_now()
    await repo.update_write_command(cmd)
    loaded = await repo.get_write_command("req-db-0001")
    assert loaded.status is WriteStatus.SUCCESS and loaded.readback_value == 12.5
    with pytest.raises(DatabaseError):
        await repo.insert_write_command(cmd)


async def test_retention_drops_expired_partitions(repo):
    today = utc_now().date()
    # create an old partition range by inserting partitions from 10 days ago
    await reset_history_partitions(repo, today - timedelta(days=10))
    old = readings(repo, [1.0], ts=utc_now() - timedelta(days=9, hours=12))
    await repo.write_batch(old, [])
    expired = await repo.expired_partitions("plc_tag_history", today - timedelta(days=5))
    assert expired
    for name in expired:
        await repo.drop_partition("plc_tag_history", name)
    assert (await repo.db.fetch_one("SELECT COUNT(*) AS n FROM plc_tag_history"))["n"] == 0


async def reset_history_partitions(repo, start):
    await repo.db.execute("ALTER TABLE plc_tag_history REMOVE PARTITIONING")
    await repo.db.execute("ALTER TABLE plc_tag_history PARTITION BY RANGE COLUMNS (gateway_ts) ("
                          "PARTITION p_initial VALUES LESS THAN ('2000-01-01'), "
                          "PARTITION p_future VALUES LESS THAN (MAXVALUE))")
    await repo.ensure_partitions("plc_tag_history", start=start, until=utc_now().date() + timedelta(days=2),
                                 monthly=False)
