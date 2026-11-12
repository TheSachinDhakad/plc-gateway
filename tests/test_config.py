"""Configuration loading and cross-validation."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from core.config_loader import load_config
from utils.exceptions import ConfigError


def copy_config(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def edit_yaml(path: Path, fn) -> None:
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_shipped_configs_are_valid(project_root):
    prod = load_config(project_root / "config")
    assert [d.device_id for d in prod.devices(enabled_only=True)] == ["siemens_01"]
    assert len(prod.tags["siemens_01"]) == 5
    assert prod.settings.gateway.read_only_mode is True          # read-only default
    sim = load_config(project_root / "config" / "sim")
    assert len(sim.devices(enabled_only=True)) == 4


def test_duplicate_tag_rejected(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")
    edit_yaml(cfg / "tags_mc.yaml", lambda d: d["tags"].append(dict(d["tags"][0])))
    with pytest.raises(ConfigError, match="duplicate tag_id speed"):
        load_config(cfg)


def test_unknown_device_and_bad_address_listed_together(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")

    def mutate(d):
        d["tags"][0]["address"] = "DB100.DBW0"           # REAL with W -> invalid
        d["tags"].append({"tag_id": "x", "device_id": "nope", "name": "x", "address": "DB1.DBD0",
                          "data_type": "REAL"})
    edit_yaml(cfg / "tags_s7.yaml", mutate)
    with pytest.raises(ConfigError) as err:
        load_config(cfg)
    text = str(err.value)
    assert "unknown device nope" in text and "size letter W" in text


def test_schema_errors_are_reported(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")
    edit_yaml(cfg / "tags_mc.yaml", lambda d: d["tags"][0].update(poll_interval_ms=10, unknown_key=1))
    with pytest.raises(ConfigError) as err:
        load_config(cfg)
    assert "poll_interval_ms" in str(err.value) and "unknown_key" in str(err.value)


def test_write_block_requires_read_write(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")
    edit_yaml(cfg / "tags_mc.yaml", lambda d: d["tags"][0].update(write={"min": 0, "max": 1}))
    with pytest.raises(ConfigError, match="write block requires access"):
        load_config(cfg)


def test_insecure_opcua_requires_explicit_flag(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")
    edit_yaml(cfg / "plants.yaml",
              lambda d: d["plants"][0]["devices"][3]["connection"].pop("allow_insecure"))
    with pytest.raises(ConfigError, match="allow_insecure"):
        load_config(cfg)


def test_fx3u_requires_1e_frame(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")
    edit_yaml(cfg / "plants.yaml",
              lambda d: d["plants"][0]["devices"][2]["connection"].update(plc_series="FX3U"))
    with pytest.raises(ConfigError, match="1E frame"):
        load_config(cfg)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_opc_da_refused_off_windows(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")

    def mutate(d):
        d["plants"][0]["devices"].append({"device_id": "da1", "name": "DA", "driver": "opc_da", "enabled": True,
                                          "tags_file": "tags_da.yaml",
                                          "connection": {"server_progid": "Kepware.KEPServerEX.V6"}})
    edit_yaml(cfg / "plants.yaml", mutate)
    (cfg / "tags_da.yaml").write_text("tags: []\n")
    with pytest.raises(ConfigError, match="not available on platform"):
        load_config(cfg)


def test_two_rtu_devices_on_same_port_rejected(tmp_path, project_root):
    cfg = copy_config(project_root / "config" / "sim", tmp_path / "c")

    def mutate(d):
        for i in (1, 2):
            d["plants"][0]["devices"].append({
                "device_id": f"rtu{i}", "name": "rtu", "driver": "modbus_rtu", "tags_file": "tags_rtu.yaml",
                "connection": {"serial_port": "/dev/ttyUSB0", "unit_id": i}})
    edit_yaml(cfg / "plants.yaml", mutate)
    (cfg / "tags_rtu.yaml").write_text("tags: []\n")
    with pytest.raises(ConfigError, match="both open serial port"):
        load_config(cfg)
