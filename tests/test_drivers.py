"""Driver tests against local protocol simulators over real sockets (marker: sim).

These prove protocol framing/decoding and error handling end to end. They do NOT prove
compatibility with physical PLCs — see tests/hardware/.
"""

from __future__ import annotations

import asyncio
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from drivers.base import BaseDriver
from drivers.mitsubishi import MitsubishiMCDriver
from drivers.modbus_tcp import ModbusTcpDriver
from drivers.opc_ua import OpcUaDriver
from drivers.siemens_s7 import SiemensS7Driver
from models.reading import Quality, WriteStatus
from simulators.mc_sim import MCSimulator
from simulators.modbus_sim import ModbusSimulator
from simulators.opcua_sim import OpcUaSimulator
from simulators.s7_sim import S7Simulator
from tests.conftest import make_device, make_tag
from utils.exceptions import DriverConnectionError

pytestmark = pytest.mark.sim


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_driver_contract_is_complete():
    for cls in (SiemensS7Driver, ModbusTcpDriver, MitsubishiMCDriver, OpcUaDriver):
        assert issubclass(cls, BaseDriver)
        assert not getattr(cls, "__abstractmethods__", None), cls


# ---------------------------------------------------------------------------------------- Modbus TCP

@pytest.fixture
async def modbus_sim():
    sim = ModbusSimulator(port=0, units=frozenset({1, 2}))
    await sim.start()
    yield sim
    await sim.stop()


def modbus_device(port: int, **conn):
    return make_device("modbus_tcp", {"host": "127.0.0.1", "port": port, "unit_id": 1, "timeout_s": 1, **conn})


async def test_modbus_batched_read_types(modbus_sim):
    modbus_sim.set_float(modbus_sim.holding, 0, 230.5)
    modbus_sim.holding[2] = 0xFFFE
    modbus_sim.holding[10] = 0b1000
    modbus_sim.input[100], modbus_sim.input[101] = 0x0001, 0x86A0
    modbus_sim.coils[3] = True
    modbus_sim.discrete[0] = True
    drv = ModbusTcpDriver(modbus_device(modbus_sim.port))
    tags = [
        make_tag("v", "holding:0", "REAL"), make_tag("i", "holding:2", "INT"),
        make_tag("b", "holding:10.3", "BOOL"), make_tag("e", "input:100", "UDINT"),
        make_tag("c", "coil:3", "BOOL"), make_tag("d", "discrete:0", "BOOL"),
        make_tag("u2", "2@holding:0", "REAL"),
    ]
    await drv.connect()
    before = modbus_sim.requests
    results = await drv.read_tags(tags)
    values = {r.tag_id: r.value for r in results}
    assert all(r.quality is Quality.GOOD for r in results)
    assert values == {"v": 230.5, "i": -2, "b": True, "e": 100000, "c": True, "d": True, "u2": 230.5}
    assert modbus_sim.requests - before == 5      # holding(unit1) / input / coil / discrete / holding(unit2)
    await drv.disconnect()


async def test_modbus_word_order_cdab(modbus_sim):
    modbus_sim.holding[0], modbus_sim.holding[1] = 0x0000, 0x4366   # 230.0 CDAB
    drv = ModbusTcpDriver(modbus_device(modbus_sim.port, word_order="little"))
    await drv.connect()
    (r,) = await drv.read_tags([make_tag("v", "holding:0", "REAL")])
    assert r.value == 230.0
    await drv.disconnect()


async def test_modbus_illegal_address_isolated(modbus_sim):
    modbus_sim.limit = 50
    drv = ModbusTcpDriver(modbus_device(modbus_sim.port, max_gap=100))
    await drv.connect()
    ok, bad = await drv.read_tags([make_tag("ok", "holding:0", "INT"), make_tag("bad", "holding:60", "INT")])
    assert ok.quality is Quality.GOOD
    assert bad.quality is Quality.BAD_ADDRESS and bad.error_code == "MODBUS_ILLEGAL_DATA_ADDRESS"
    await drv.disconnect()


async def test_modbus_writes(modbus_sim):
    drv = ModbusTcpDriver(modbus_device(modbus_sim.port))
    await drv.connect()
    real = make_tag("sp", "holding:20", "REAL", access="read_write")
    coil = make_tag("cmd", "coil:7", "BOOL", access="read_write")
    assert (await drv.write_tag(real, 42.25)).status is WriteStatus.SUCCESS
    assert struct.unpack(">f", struct.pack(">HH", *modbus_sim.holding[20:22]))[0] == 42.25
    assert (await drv.write_tag(coil, True)).status is WriteStatus.SUCCESS
    assert modbus_sim.coils[7] is True
    assert (await drv.read_tag(real)).value == 42.25
    await drv.disconnect()


async def test_modbus_connection_loss_raises(modbus_sim):
    drv = ModbusTcpDriver(modbus_device(modbus_sim.port))
    await drv.connect()
    await modbus_sim.stop()
    with pytest.raises(DriverConnectionError):
        for _ in range(3):
            await drv.read_tags([make_tag("v", "holding:0", "INT")])
            await asyncio.sleep(0.1)
    await drv.disconnect()


async def test_modbus_connect_refused():
    drv = ModbusTcpDriver(modbus_device(free_port()))
    with pytest.raises(DriverConnectionError):
        await drv.connect()


# ---------------------------------------------------------------------------------------- Mitsubishi MC

@pytest.fixture
async def mc_sim():
    sim = MCSimulator(port=0)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.mark.parametrize("frame,series", [("3E", "FX5U"), ("3E", "Q"), ("3E", "iQ-R"), ("1E", "FX3U")])
async def test_mc_read_write(mc_sim, frame, series):
    dev = make_device("mitsubishi_mc", {"host": "127.0.0.1", "port": mc_sim.port, "frame": frame,
                                        "plc_series": series, "timeout_s": 1})
    mc_sim.words["D"][100] = 1234
    mc_sim.words["D"][200], mc_sim.words["D"][201] = struct.unpack("<HH", struct.pack("<i", -70000))
    mc_sim.bits["M"][10] = True
    mc_sim.bits["X"][15] = True
    drv = MitsubishiMCDriver(dev)
    await drv.connect()
    x_addr = "X17" if series in ("FX3U", "FX5U") else "XF"
    tags = [make_tag("s", "D100", "INT"), make_tag("c", "D200", "DINT"), make_tag("m", "M10", "BOOL"),
            make_tag("x", x_addr, "BOOL"), make_tag("arr", "D100", "INT", array_length=2)]
    res = {r.tag_id: r for r in await drv.read_tags(tags)}
    assert {k: v.value for k, v in res.items()} == {"s": 1234, "c": -70000, "m": True, "x": True, "arr": [1234, 0]}
    w = make_tag("w", "D300", "REAL", access="read_write")
    assert (await drv.write_tag(w, 12.5)).status is WriteStatus.SUCCESS
    assert (await drv.read_tag(w)).value == 12.5
    b = make_tag("y", "Y0", "BOOL", access="read_write")
    assert (await drv.write_tag(b, True)).status is WriteStatus.SUCCESS
    assert mc_sim.bits["Y"][0] is True
    await drv.disconnect()


async def test_mc_device_error_is_per_block_not_connection(mc_sim):
    dev = make_device("mitsubishi_mc", {"host": "127.0.0.1", "port": mc_sim.port, "frame": "3E",
                                        "plc_series": "FX5U", "timeout_s": 1})
    drv = MitsubishiMCDriver(dev)
    await drv.connect()
    good, bad = await drv.read_tags([make_tag("g", "D0", "INT"), make_tag("b", "D65535", "DINT")])
    assert good.quality is Quality.GOOD
    assert bad.quality is Quality.BAD_DEVICE_FAILURE and bad.error_code == "MC_END_0xC056"
    assert drv.is_connected()
    info = await drv.get_device_info()
    assert info.model == "FX5U-32MT/ES"
    await drv.disconnect()


# ---------------------------------------------------------------------------------------- Siemens S7

@pytest.fixture
async def s7_sim():
    sim = S7Simulator(port=free_port())
    sim.start()
    await asyncio.sleep(0.2)
    yield sim
    await sim.stop()


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1)
    yield ex
    ex.shutdown(wait=False)


async def test_s7_read_write_db100(s7_sim, executor):
    s7_sim.set_real(0, 25.5)
    s7_sim.set_real(4, 4.25)
    s7_sim.set_int(8, -42)
    s7_sim.set_bool(10, 0, True)
    dev = make_device("siemens_s7", {"host": "127.0.0.1", "port": s7_sim.port, "rack": 0, "slot": 1,
                                     "timeout_s": 2})
    drv = SiemensS7Driver(dev, executor=executor)
    await drv.connect()
    tags = [make_tag("t", "DB100.DBD0", "REAL"), make_tag("p", "DB100.DBD4", "REAL"),
            make_tag("c", "DB100.DBW8", "INT"), make_tag("m", "DB100.DBX10.0", "BOOL"),
            make_tag("m1", "DB100.DBX10.1", "BOOL")]
    res = {r.tag_id: r.value for r in await drv.read_tags(tags)}
    assert res == {"t": 25.5, "p": 4.25, "c": -42, "m": True, "m1": False}

    real = make_tag("w", "DB100.DBD12", "REAL", access="read_write")
    assert (await drv.write_tag(real, 12.75)).status is WriteStatus.SUCCESS
    assert struct.unpack(">f", s7_sim.db100[12:16])[0] == 12.75
    bit = make_tag("wb", "DB100.DBX10.3", "BOOL", access="read_write")
    assert (await drv.write_tag(bit, True)).status is WriteStatus.SUCCESS
    # NOTE: snap7's bundled Server ignores the bit index of BIT writes (writes the whole byte), so
    # server memory is not asserted here; the request encoding is verified in
    # test_s7_bit_write_request_encoding and must be confirmed on hardware.
    await drv.disconnect()


def test_s7_bit_write_request_encoding():
    from snap7.datatypes import S7Area, S7DataTypes, S7WordLen
    spec = S7DataTypes.encode_address(S7Area.DB, 100, 10 * 8 + 3, S7WordLen.BIT, 1)
    # ... transport size 0x01 (BIT), count 1, DB 100, area 0x84, address = byte 10 << 3 | bit 3
    assert spec.hex().endswith("01" + "0001" + "0064" + "84" + "000053")


async def test_s7_missing_db_isolated(s7_sim, executor):
    dev = make_device("siemens_s7", {"host": "127.0.0.1", "port": s7_sim.port, "timeout_s": 2})
    drv = SiemensS7Driver(dev, executor=executor)
    await drv.connect()
    ok, missing = await drv.read_tags([make_tag("ok", "DB100.DBD0", "REAL"), make_tag("x", "DB999.DBD0", "REAL")])
    assert ok.quality is Quality.GOOD
    assert missing.quality.is_bad
    assert drv.is_connected()
    await drv.disconnect()


async def test_s7_connect_refused(executor):
    dev = make_device("siemens_s7", {"host": "127.0.0.1", "port": free_port(), "timeout_s": 1})
    with pytest.raises(DriverConnectionError):
        await SiemensS7Driver(dev, executor=executor).connect()


# ---------------------------------------------------------------------------------------- OPC UA

@pytest.fixture
async def ua_sim():
    sim = OpcUaSimulator(port=free_port())
    await sim.start()
    yield sim
    await sim.stop()


def ua_device(sim):
    return make_device("opc_ua", {"endpoint": sim.endpoint, "security_policy": "None", "security_mode": "None",
                                  "allow_insecure": True, "timeout_s": 3})


async def test_opcua_read_types_timestamps_and_write(ua_sim):
    drv = OpcUaDriver(ua_device(ua_sim))
    await drv.connect()
    tags = [make_tag("t", ua_sim.node_id("Temperature"), "REAL"), make_tag("c", ua_sim.node_id("Counter"), "DINT"),
            make_tag("r", ua_sim.node_id("Running"), "BOOL"), make_tag("n", ua_sim.node_id("Name"), "STRING"),
            make_tag("missing", "ns=2;s=Sim.DoesNotExist", "REAL"),
            make_tag("wrong", ua_sim.node_id("Name"), "REAL")]
    res = {r.tag_id: r for r in await drv.read_tags(tags)}
    assert res["t"].value == pytest.approx(25.0) and res["t"].quality is Quality.GOOD
    assert res["c"].value == 0 and res["r"].value is False and res["n"].value == "Line 1"
    assert res["t"].timestamp_origin == "device"
    assert res["missing"].quality is Quality.BAD_ADDRESS
    assert res["wrong"].quality is Quality.BAD_TYPE_MISMATCH
    sp = make_tag("sp", ua_sim.node_id("Setpoint"), "REAL", access="read_write")
    assert (await drv.write_tag(sp, 61.5)).status is WriteStatus.SUCCESS
    assert (await drv.read_tag(sp)).value == 61.5
    ro = make_tag("ro", ua_sim.node_id("Counter"), "DINT", access="read_write")
    assert (await drv.write_tag(ro, 5)).status is WriteStatus.FAILED     # not writable on server
    info = await drv.get_device_info()
    assert "urn:industrial-plc-gateway:sim" in info.extra["namespaces"]
    await drv.disconnect()


async def test_opcua_server_down_raises(ua_sim):
    drv = OpcUaDriver(ua_device(ua_sim))
    await drv.connect()
    await ua_sim.stop()
    with pytest.raises(DriverConnectionError):
        for _ in range(5):
            await drv.read_tags([make_tag("t", ua_sim.node_id("Temperature"), "REAL")])
            await asyncio.sleep(0.3)
    await drv.disconnect()
