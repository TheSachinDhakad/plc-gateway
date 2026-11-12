"""Driver logic without a server: Rockwell (fake pycomm3 client), Modbus RTU, OPC DA mapping."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from pycomm3 import Tag
from pycomm3.exceptions import CommError

import drivers.rockwell as rockwell
from drivers.modbus_rtu import ModbusRtuDriver
from drivers.opc_da import map_da_quality
from drivers.serial import rtu_inter_frame_delay_s
from models.reading import Quality, WriteStatus
from tests.conftest import make_device, make_tag
from utils.exceptions import DriverConnectionError, TagConfigError


class FakeLogix:
    """Mimics the pycomm3.LogixDriver surface used by the driver."""

    tags_db = {"Line1_Temp": "REAL", "Counter": "DINT", "Running": "BOOL", "Recipe": "REAL", "Name": "STRING",
               "Program:Main.Speed": "INT"}

    def __init__(self, path, init_tags=True, init_program_tags=True):
        self.path = path
        self.connected = False
        self.socket_timeout = None
        self.values = {"Line1_Temp": 21.5, "Counter": 42, "Running": True, "Name": "LINE-1",
                       "Program:Main.Speed": 1500, "Recipe[0]{3}": [1.0, 2.0, 3.0]}
        self.fail_comm = False
        self.written = []

    @property
    def tags(self):
        return {name: {} for name in self.tags_db}

    @property
    def info(self):
        return {"vendor": "Rockwell Automation/Allen-Bradley", "product_name": "1756-L83E/B",
                "revision": {"major": 33, "minor": 11}, "serial": "c0ffee01"}

    def open(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def read(self, *names):
        if self.fail_comm:
            self.connected = False
            raise CommError("socket closed")
        out = []
        for n in names:
            if n in self.values:
                v = self.values[n]
                out.append(Tag(n, v, self.tags_db.get(n.split("[")[0], "REAL"), None))
            else:
                out.append(Tag(n, None, None, "Path segment error"))
        return out if len(out) > 1 else out[0]

    def write(self, *items):
        self.written.extend(items)
        return [Tag(n, v, "REAL", None) for n, v in items] if len(items) > 1 else Tag(items[0][0], items[0][1],
                                                                                     "REAL", None)

    def get_plc_time(self):
        return Tag("__GET_PLC_TIME__", 0, None, None)


@pytest.fixture
def logix(monkeypatch):
    monkeypatch.setattr(rockwell, "LogixDriver", FakeLogix)
    ex = ThreadPoolExecutor(max_workers=1)
    drv = rockwell.RockwellLogixDriver(make_device("rockwell_logix", {"path": "10.0.0.5/0"}), executor=ex)
    yield drv
    ex.shutdown(wait=False)


async def test_logix_read_normalization(logix):
    tags = [make_tag("t", "Line1_Temp", "REAL"), make_tag("c", "Counter", "DINT"),
            make_tag("r", "Running", "BOOL"), make_tag("n", "Name", "STRING"),
            make_tag("s", "Program:Main.Speed", "INT"), make_tag("a", "Recipe[0]", "REAL", array_length=3),
            make_tag("missing", "NoSuchTag", "REAL"), make_tag("wrong", "Name", "REAL")]
    await logix.register_tags(tags)
    await logix.connect()
    res = {r.tag_id: r for r in await logix.read_tags(tags)}
    assert res["t"].value == 21.5 and res["c"].value == 42 and res["r"].value is True
    assert res["n"].value == "LINE-1" and res["s"].value == 1500 and res["a"].value == [1.0, 2.0, 3.0]
    assert res["missing"].quality is Quality.BAD_ADDRESS and res["missing"].error_code == "LOGIX_TAG_NOT_FOUND"
    assert res["wrong"].quality is Quality.BAD_TYPE_MISMATCH
    info = await logix.get_device_info()
    assert info.model == "1756-L83E/B" and info.firmware == "33.11"


async def test_logix_comm_error_raises_connection_error(logix):
    tag = make_tag("t", "Line1_Temp", "REAL")
    await logix.register_tags([tag])
    await logix.connect()
    logix._plc.fail_comm = True
    with pytest.raises(DriverConnectionError):
        await logix.read_tags([tag])


async def test_logix_write(logix):
    tag = make_tag("t", "Line1_Temp", "REAL", access="read_write")
    await logix.register_tags([tag])
    await logix.connect()
    result = await logix.write_tag(tag, 22.0)
    assert result.status is WriteStatus.SUCCESS
    assert logix._plc.written == [("Line1_Temp", 22.0)]


def test_logix_validation():
    dev = make_device("rockwell_logix", {"path": "10.0.0.5/0"})
    rockwell.RockwellLogixDriver.validate_tag(dev, make_tag("ok", "Program:Main.Speed", "INT"))
    with pytest.raises(TagConfigError):
        rockwell.RockwellLogixDriver.validate_tag(dev, make_tag("bad", "a b", "INT"))
    with pytest.raises(TagConfigError):
        rockwell.RockwellLogixDriver.validate_tag(dev, make_tag("bad", "X", "WSTRING"))


async def test_modbus_rtu_missing_port_is_connection_error():
    dev = make_device("modbus_rtu", {"serial_port": "/dev/does-not-exist-plcgw", "unit_id": 1})
    with pytest.raises(DriverConnectionError, match="not found"):
        await ModbusRtuDriver(dev).connect()


def test_modbus_rtu_validation_rejects_broadcast_and_readonly_writes():
    dev = make_device("modbus_rtu", {"serial_port": "COM3", "unit_id": 1})
    with pytest.raises(TagConfigError):
        ModbusRtuDriver.validate_tag(dev, make_tag("b", "0@holding:0", "INT"))
    with pytest.raises(TagConfigError):
        ModbusRtuDriver.validate_tag(dev, make_tag("w", "input:0", "INT", access="read_write"))


def test_rtu_frame_timing():
    assert rtu_inter_frame_delay_s(9600, 8, "E", 1) == pytest.approx(3.5 * 11 / 9600)
    assert rtu_inter_frame_delay_s(115200) == 0.00175


@pytest.mark.parametrize("quality,expected", [
    (0xC0, Quality.GOOD), (0xD8, Quality.GOOD), (0x40, Quality.UNCERTAIN), (0x18, Quality.BAD_COMM_FAILURE),
    (0x0C, Quality.BAD_DEVICE_FAILURE), (0x04, Quality.BAD_CONFIG_ERROR), (0x08, Quality.BAD_NOT_CONNECTED),
    (0x00, Quality.BAD),
])
def test_opc_da_quality_mapping(quality, expected):
    assert map_da_quality(quality) is expected
