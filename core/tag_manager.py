"""Tag registry (lookup by device/tag) and the in-memory latest-value cache."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable

from models.config import GatewayConfig
from models.plc import DeviceConfig
from models.reading import TagReading
from models.tag import TagConfig


class TagManager:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._devices: dict[str, DeviceConfig] = {d.device_id: d for d in config.devices()}
        self._tags: dict[tuple[str, str], TagConfig] = {}
        self._by_tag_id: dict[str, list[TagConfig]] = defaultdict(list)
        for device_id, tags in config.tags.items():
            for tag in tags:
                self._tags[(device_id, tag.tag_id)] = tag
                self._by_tag_id[tag.tag_id].append(tag)
        self._latest: dict[tuple[str, str], TagReading] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ registry
    def device(self, device_id: str) -> DeviceConfig | None:
        return self._devices.get(device_id)

    def devices(self) -> list[DeviceConfig]:
        return list(self._devices.values())

    def tag(self, device_id: str, tag_id: str) -> TagConfig | None:
        return self._tags.get((device_id, tag_id))

    def tags_for_device(self, device_id: str, *, enabled_only: bool = False) -> list[TagConfig]:
        tags = self._config.tags.get(device_id, [])
        return [t for t in tags if t.enabled] if enabled_only else list(tags)

    def find_tag(self, tag_id: str) -> list[TagConfig]:
        return list(self._by_tag_id.get(tag_id, []))

    def all_tags(self) -> Iterable[TagConfig]:
        return self._tags.values()

    def scan_classes(self, device_id: str) -> dict[int, list[TagConfig]]:
        groups: dict[int, list[TagConfig]] = defaultdict(list)
        for tag in self.tags_for_device(device_id, enabled_only=True):
            groups[tag.poll_interval_ms].append(tag)
        return dict(sorted(groups.items()))

    # ------------------------------------------------------------------ latest values
    def update_latest(self, readings: Iterable[TagReading]) -> None:
        with self._lock:
            for r in readings:
                self._latest[(r.device_id, r.tag_id)] = r

    def latest(self, device_id: str | None = None, tag_id: str | None = None) -> list[TagReading]:
        with self._lock:
            values = list(self._latest.values())
        if device_id:
            values = [r for r in values if r.device_id == device_id]
        if tag_id:
            values = [r for r in values if r.tag_id == tag_id]
        return sorted(values, key=lambda r: (r.device_id, r.tag_id))

    def latest_one(self, device_id: str, tag_id: str) -> TagReading | None:
        with self._lock:
            return self._latest.get((device_id, tag_id))
