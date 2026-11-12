"""Write safety gates, idempotency, audit, uncertain outcomes, read-back verification."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.config_loader import load_config
from core.tag_manager import TagManager
from core.write_manager import Principal, WriteCommand, WriteManager
from models.reading import Quality, RawReadResult, WriteResult, WriteStatus
from utils.exceptions import WriteRejectedError
from utils.timeutil import utc_now

OPERATOR = Principal("alice", frozenset({"operator"}))
VIEWER = Principal("bob", frozenset({"viewer"}))
ROOT = Path(__file__).resolve().parent.parent


class FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.commands: dict[str, WriteCommand] = {}
        self.audit: list[dict] = []
        self.fail = fail

    async def get_write_command(self, request_id):
        if self.fail:
            raise RuntimeError("db down")
        return self.commands.get(request_id)

    async def insert_write_command(self, cmd):
        if self.fail:
            raise RuntimeError("db down")
        self.commands[cmd.request_id] = cmd

    async def update_write_command(self, cmd):
        self.commands[cmd.request_id] = cmd

    async def insert_audit(self, **kw):
        self.audit.append(kw)


class FakeDriver:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


class FakeTarget:
    device_id = "s7_sim"

    def __init__(self, *, result=WriteStatus.SUCCESS, readback=None, hang=False, connected=True):
        self.driver = FakeDriver(connected)
        self.result = result
        self.readback = readback
        self.hang = hang
        self.writes = []

    async def write(self, tag, value, timeout_s):
        self.writes.append(value)
        if self.hang:
            await asyncio.wait_for(asyncio.sleep(10), timeout_s)
        return WriteResult(tag.tag_id, self.result)

    async def read_now(self, tag, timeout_s):
        value = self.writes[-1] if self.readback is None else self.readback
        return RawReadResult(tag.tag_id, value, Quality.GOOD, utc_now())


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_WRITE_DISABLE", raising=False)
    cfg = load_config(ROOT / "config" / "sim")
    cfg = cfg.model_copy(update={"config_dir": tmp_path / "config"})   # kill-switch file under tmp
    return cfg


def manager(config, target=None, store=None):
    target = target or FakeTarget()
    return WriteManager(config, TagManager(config), store or FakeStore(), {"s7_sim": target}), target


async def test_successful_write_with_readback_and_audit(config):
    store = FakeStore()
    wm, target = manager(config, store=store)
    cmd = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 42.5, "req-0000001", "commissioning")
    assert cmd.status is WriteStatus.SUCCESS and cmd.readback_value == 42.5
    assert target.writes == [42.5]
    assert store.commands["req-0000001"].status is WriteStatus.SUCCESS
    assert store.audit[-1]["result"] == "SUCCESS" and store.audit[-1]["principal"] == "alice"


async def test_idempotent_request_id_not_reexecuted(config):
    wm, target = manager(config)
    a = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 10.0, "req-0000002")
    b = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 10.0, "req-0000002")
    assert a is b and target.writes == [10.0]
    with pytest.raises(WriteRejectedError) as err:
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 11.0, "req-0000002")
    assert err.value.code == "REQUEST_ID_CONFLICT"


@pytest.mark.parametrize("principal,device,tag,value,code", [
    (VIEWER, "s7_sim", "test_write_value", 1.0, "FORBIDDEN"),
    (OPERATOR, "s7_sim", "temperature", 1.0, "FORBIDDEN"),          # read-only tag: no write roles
    (OPERATOR, "s7_sim", "test_write_value", 101.0, "ABOVE_MAX"),
    (OPERATOR, "s7_sim", "test_write_value", -1.0, "BELOW_MIN"),
    (OPERATOR, "s7_sim", "test_write_value", "5", "INVALID_TYPE"),
    (OPERATOR, "nope", "x", 1.0, "NOT_FOUND"),
])
async def test_rejections(config, principal, device, tag, value, code):
    wm, target = manager(config)
    with pytest.raises(WriteRejectedError) as err:
        await wm.submit(principal, device, tag, value, "req-0000003")
    assert err.value.code == code
    assert target.writes == []


async def test_global_read_only_and_kill_switches(config, monkeypatch):
    ro = config.model_copy(deep=True)
    ro.settings.gateway.read_only_mode = True
    wm, target = manager(ro)
    with pytest.raises(WriteRejectedError, match="GATEWAY_READ_ONLY"):
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000004")

    wm, target = manager(config)
    monkeypatch.setenv("GATEWAY_WRITE_DISABLE", "1")
    with pytest.raises(WriteRejectedError, match="KILL_SWITCH_ENV"):
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000005")
    monkeypatch.delenv("GATEWAY_WRITE_DISABLE")

    kill = config.resolve_path(config.settings.gateway.write_kill_switch_file)
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.touch()
    with pytest.raises(WriteRejectedError, match="KILL_SWITCH_FILE"):
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000006")
    kill.unlink()
    assert target.writes == []


async def test_device_write_disabled(config):
    cfg = config.model_copy(deep=True)
    cfg.plants[0].devices[0].write_enabled = False
    wm, _ = manager(cfg)
    with pytest.raises(WriteRejectedError) as err:
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000007")
    assert err.value.code == "DEVICE_READ_ONLY"


async def test_audit_store_down_fails_closed(config):
    wm, target = manager(config, store=FakeStore(fail=True))
    with pytest.raises(WriteRejectedError) as err:
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000008")
    assert err.value.code == "AUDIT_UNAVAILABLE" and target.writes == []


async def test_timeout_is_uncertain_and_not_retried(config):
    cfg = config.model_copy(deep=True)
    cfg.settings.writes.default_timeout_s = 0.2
    wm, target = manager(cfg, target=FakeTarget(hang=True))
    cmd = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 3.0, "req-0000009")
    assert cmd.status is WriteStatus.UNCERTAIN and cmd.error_code == "WRITE_TIMEOUT"
    again = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 3.0, "req-0000009")
    assert again.status is WriteStatus.UNCERTAIN and len(target.writes) == 1     # never replayed


async def test_readback_mismatch(config):
    wm, _ = manager(config, target=FakeTarget(readback=7.0))
    cmd = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 3.0, "req-0000010")
    assert cmd.status is WriteStatus.MISMATCH and cmd.readback_value == 7.0


async def test_disconnected_device(config):
    wm, target = manager(config, target=FakeTarget(connected=False))
    cmd = await wm.submit(OPERATOR, "s7_sim", "test_write_value", 3.0, "req-0000011")
    assert cmd.status is WriteStatus.FAILED and cmd.error_code == "DEVICE_NOT_CONNECTED"
    assert target.writes == []


async def test_rate_limit(config):
    cfg = config.model_copy(deep=True)
    cfg.settings.writes.rate_limit_per_min = 2
    wm, _ = manager(cfg)
    await wm.submit(OPERATOR, "s7_sim", "test_write_value", 1.0, "req-0000012")
    await wm.submit(OPERATOR, "s7_sim", "test_write_value", 2.0, "req-0000013")
    with pytest.raises(WriteRejectedError) as err:
        await wm.submit(OPERATOR, "s7_sim", "test_write_value", 3.0, "req-0000014")
    assert err.value.code == "RATE_LIMITED"
