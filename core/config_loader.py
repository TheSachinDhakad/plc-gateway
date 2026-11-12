"""Load and validate YAML configuration before any PLC connection is opened."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from drivers import get_driver_class
from models.config import GatewayConfig, Settings
from models.plc import DriverName, PlantsFile
from models.tag import TagConfig, TagFile
from utils.exceptions import ConfigError, GatewayError

log = logging.getLogger(__name__)


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc


def _format_validation(source: str, exc: ValidationError) -> str:
    lines = [f"{source}: {exc.error_count()} validation error(s)"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_env(env_file: Path | None) -> None:
    """Load ``.env`` without overriding variables already set in the environment."""
    if env_file is not None and env_file.is_file():
        load_dotenv(env_file, override=False)


def config_fingerprint(config_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(config_dir.glob("*.yaml")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_config(config_dir: Path, *, env_file: Path | None = None, check_drivers: bool = True) -> GatewayConfig:
    """Parse settings, plants and tag files; run cross-file and driver-level validation.

    Raises :class:`ConfigError` with every problem listed.
    """
    config_dir = config_dir.resolve()
    load_env(env_file)
    errors: list[str] = []

    try:
        settings = Settings.model_validate(_read_yaml(config_dir / "settings.yaml"))
    except ValidationError as exc:
        raise ConfigError(_format_validation("settings.yaml", exc)) from exc
    try:
        plants_file = PlantsFile.model_validate(_read_yaml(config_dir / "plants.yaml"))
    except ValidationError as exc:
        raise ConfigError(_format_validation("plants.yaml", exc)) from exc

    plant_ids: set[str] = set()
    devices = {}
    serial_ports: dict[str, str] = {}
    for plant in plants_file.plants:
        if plant.plant_id in plant_ids:
            errors.append(f"plants.yaml: duplicate plant_id {plant.plant_id}")
        plant_ids.add(plant.plant_id)
        for device in plant.devices:
            if device.device_id in devices:
                errors.append(f"plants.yaml: duplicate device_id {device.device_id}")
            devices[device.device_id] = device
            if device.driver is DriverName.MODBUS_RTU and device.enabled and plant.enabled:
                port = device.connection.serial_port.lower()  # type: ignore[union-attr]
                if port in serial_ports:
                    errors.append(
                        f"devices {serial_ports[port]} and {device.device_id} both open serial port "
                        f"{port}; use one device per bus with 'unit@' tag address prefixes")
                serial_ports[port] = device.device_id

    tags_by_device: dict[str, list[TagConfig]] = {d: [] for d in devices}
    loaded_files: dict[str, list[TagConfig]] = {}
    for device in devices.values():
        rel = device.tags_file
        if rel in loaded_files:
            continue
        path = (config_dir / rel).resolve()
        if config_dir not in path.parents and path.parent != config_dir:
            errors.append(f"{device.device_id}: tags_file must be inside the config directory")
            loaded_files[rel] = []
            continue
        try:
            loaded_files[rel] = TagFile.model_validate(_read_yaml(path)).tags
        except ValidationError as exc:
            errors.append(_format_validation(rel, exc))
            loaded_files[rel] = []
        except ConfigError as exc:
            errors.append(str(exc))
            loaded_files[rel] = []

    for rel, tags in loaded_files.items():
        for tag in tags:
            device = devices.get(tag.device_id)
            if device is None:
                errors.append(f"{rel}: tag {tag.tag_id} references unknown device {tag.device_id}")
                continue
            if device.tags_file != rel:
                # a shared tag file is only valid if the device points at it
                if not any(d.tags_file == rel and d.device_id == tag.device_id for d in devices.values()):
                    errors.append(f"{rel}: tag {tag.tag_id} belongs to {tag.device_id} whose tags_file is "
                                  f"{device.tags_file}")
                    continue
            tags_by_device[tag.device_id].append(tag)

    for device_id, tags in tags_by_device.items():
        seen: set[str] = set()
        for tag in tags:
            if tag.tag_id in seen:
                errors.append(f"device {device_id}: duplicate tag_id {tag.tag_id}")
            seen.add(tag.tag_id)

    if check_drivers:
        for plant in plants_file.plants:
            for device in plant.devices:
                if not (plant.enabled and device.enabled):
                    continue
                try:
                    driver_cls = get_driver_class(device.driver)
                    driver_cls.check_platform()
                except GatewayError as exc:
                    errors.append(f"device {device.device_id}: {exc}")
                    continue
                for tag in tags_by_device[device.device_id]:
                    if not tag.enabled:
                        continue
                    try:
                        driver_cls.validate_tag(device, tag)
                    except GatewayError as exc:
                        errors.append(f"device {device.device_id} tag {tag.tag_id}: {exc}")
                    if tag.access.value == "read_write" and not driver_cls.supports_write:
                        errors.append(f"device {device.device_id} tag {tag.tag_id}: driver does not support writes")

    if errors:
        raise ConfigError("configuration invalid:\n" + "\n".join(errors))

    cfg = GatewayConfig(config_dir=config_dir, settings=settings, plants=plants_file.plants,
                        tags=tags_by_device)
    enabled = cfg.devices(enabled_only=True)
    tag_count = sum(1 for d in enabled for t in cfg.tags[d.device_id] if t.enabled)
    log.info("configuration valid: %d plants, %d enabled devices, %d enabled tags",
             len(cfg.plants), len(enabled), tag_count)
    return cfg


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"environment variable {name} is not set")
    return value
