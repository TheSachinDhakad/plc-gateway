"""Instantiate drivers for enabled devices and own their dedicated executors."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from drivers import get_driver_class
from drivers.base import BaseDriver
from models.plc import DeviceConfig

log = logging.getLogger(__name__)


class DriverManager:
    def __init__(self) -> None:
        self._drivers: dict[str, BaseDriver] = {}
        self._executors: dict[str, ThreadPoolExecutor] = {}

    def create(self, device: DeviceConfig) -> BaseDriver:
        if device.device_id in self._drivers:
            return self._drivers[device.device_id]
        cls = get_driver_class(device.driver)
        executor = None
        if cls.blocking:
            # exactly one thread per blocking device: serializes access to the
            # non-thread-safe client and bounds total thread count
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"drv-{device.device_id}")
            self._executors[device.device_id] = executor
        driver = cls(device, executor=executor)
        self._drivers[device.device_id] = driver
        return driver

    def get(self, device_id: str) -> BaseDriver | None:
        return self._drivers.get(device_id)

    def all(self) -> dict[str, BaseDriver]:
        return dict(self._drivers)

    async def shutdown(self) -> None:
        for device_id, driver in self._drivers.items():
            try:
                await driver.disconnect()
            except Exception as exc:
                log.warning("disconnect failed for %s: %s", device_id, exc)
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        self._drivers.clear()
        self._executors.clear()
