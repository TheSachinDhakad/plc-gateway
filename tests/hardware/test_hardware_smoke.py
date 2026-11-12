"""Hardware smoke tests against a REAL PLC (marker: hardware; deselected by default).

Run on the plant network during commissioning, e.g. for the S7-1200 test configuration:

    PLC_HW_CONFIG_DIR=config PLC_HW_DEVICE=siemens_01 pytest -m hardware tests/hardware -v
    # optional test write to a scratch tag (still subject to every gateway write gate):
    PLC_HW_WRITE_TAG=test_write_value PLC_HW_WRITE_VALUE=12.5 ...

Checks: connect, device info, read every enabled tag in one batch, all GOOD quality, repeated
reads stable, disconnect/reconnect, optional test write with read-back. Record CPU model,
firmware and the result in docs/COMPATIBILITY.md before marking a model HW-Verified.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.config_loader import load_config
from drivers import get_driver_class
from models.reading import Quality, WriteStatus

pytestmark = pytest.mark.hardware
CONFIG_DIR = os.environ.get("PLC_HW_CONFIG_DIR")
DEVICE_ID = os.environ.get("PLC_HW_DEVICE")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def setup():
    if not (CONFIG_DIR and DEVICE_ID):
        pytest.skip("set PLC_HW_CONFIG_DIR and PLC_HW_DEVICE")
    config = load_config(ROOT / CONFIG_DIR)
    device = config.device(DEVICE_ID)
    assert device is not None, f"device {DEVICE_ID} not in config"
    tags = [t for t in config.tags[DEVICE_ID] if t.enabled]
    cls = get_driver_class(device.driver)
    executor = ThreadPoolExecutor(max_workers=1) if cls.blocking else None
    driver = cls(device, executor=executor)
    await driver.register_tags(tags)
    yield config, device, driver, tags
    await driver.disconnect()
    if executor:
        executor.shutdown(wait=False)


async def test_connect_info_and_batch_read(setup):
    _, device, driver, tags = setup
    t0 = time.perf_counter()
    await driver.connect()
    connect_ms = (time.perf_counter() - t0) * 1000
    info = await driver.get_device_info()
    t0 = time.perf_counter()
    results = await driver.read_tags(tags)
    read_ms = (time.perf_counter() - t0) * 1000
    report = {"device": device.device_id, "driver": str(device.driver), "model": info.model,
              "firmware": info.firmware, "serial": info.serial_number, "extra": info.extra,
              "connect_ms": round(connect_ms, 1), "batch_read_ms": round(read_ms, 1), "tags": len(tags),
              "results": {r.tag_id: [r.value, r.quality.value, r.error_code] for r in results}}
    print(json.dumps(report, indent=2, default=str))
    bad = {r.tag_id: (r.quality.value, r.error_code, r.error_message) for r in results if r.quality is not Quality.GOOD}
    assert not bad, f"tags with non-GOOD quality: {bad}"


async def test_repeated_reads_and_reconnect(setup):
    _, _, driver, tags = setup
    await driver.connect()
    for _ in range(20):
        results = await driver.read_tags(tags)
        assert all(r.quality is Quality.GOOD for r in results)
    await driver.disconnect()
    assert not driver.is_connected()
    await driver.connect()
    assert all(r.quality is Quality.GOOD for r in await driver.read_tags(tags))


async def test_optional_scratch_write(setup):
    tag_id = os.environ.get("PLC_HW_WRITE_TAG")
    if not tag_id:
        pytest.skip("PLC_HW_WRITE_TAG not set (write test is opt-in)")
    config, device, driver, _tags = setup
    tag = next(t for t in config.tags[device.device_id] if t.tag_id == tag_id)
    assert tag.access.value == "read_write" and device.write_enabled, "tag/device not write-enabled in config"
    value = json.loads(os.environ["PLC_HW_WRITE_VALUE"])
    await driver.connect()
    result = await driver.write_tag(tag, value)
    assert result.status is WriteStatus.SUCCESS, result
    readback = await driver.read_tag(tag)
    assert readback.quality is Quality.GOOD and readback.value == pytest.approx(value)
