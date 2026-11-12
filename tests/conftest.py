from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.plc import DeviceConfig  # noqa: E402
from models.tag import TagConfig  # noqa: E402


def make_device(driver: str, connection: dict[str, Any], **extra: Any) -> DeviceConfig:
    data = {"device_id": extra.pop("device_id", "dev1"), "name": "Test device", "driver": driver,
            "connection": connection, **extra}
    device = DeviceConfig.model_validate(data)
    device.plant_id = "plant_t"
    return device


def make_tag(tag_id: str, address: str, data_type: str, device_id: str = "dev1", **extra: Any) -> TagConfig:
    return TagConfig.model_validate({"tag_id": tag_id, "device_id": device_id, "name": tag_id, "address": address,
                                     "data_type": data_type, **extra})


@pytest.fixture
def project_root() -> Path:
    return ROOT
