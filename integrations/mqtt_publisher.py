"""Publish readings/events to an MQTT broker (optional dependency ``paho-mqtt>=2``).

Topics: ``{topic_prefix}/{plant_id}/{device_id}/{tag_id}/reading`` and
``{topic_prefix}/{plant_id}/{device_id}/events``. Payloads are the normalized JSON dicts.
TLS is on by default. Delivery buffer is in memory (bounded); for durable delivery to a
central server use the REST forwarder with its SQLite outbox.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from integrations.base import AdapterHealth, IntegrationAdapter
from models.config import MqttPublisherConfig
from models.events import TagEvent
from models.reading import TagReading
from utils.exceptions import ConfigError

log = logging.getLogger(__name__)


class MqttPublisher(IntegrationAdapter):
    type = "mqtt_publisher"

    def __init__(self, cfg: MqttPublisherConfig, ca_file: Path | None) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise ConfigError("mqtt_publisher requires 'paho-mqtt' (pip install paho-mqtt==2.2.0)") from exc
        self._mqtt = mqtt
        self.cfg = cfg
        self.name = cfg.name
        self.publish_readings_enabled = "readings" in cfg.publish
        self.publish_events_enabled = "events" in cfg.publish
        self._ca_file = ca_file
        self._client: Any = None
        self.delivered = self.failed = self.dropped = 0
        self.last_error: str | None = None
        self._connected = False

    async def start(self) -> None:
        mqtt = self._mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self.cfg.client_id or f"plc-gateway-{uuid.uuid4().hex[:8]}")
        client.max_queued_messages_set(self.cfg.queue_max_size)
        if self.cfg.username_env:
            client.username_pw_set(os.environ.get(self.cfg.username_env, ""),
                                   os.environ.get(self.cfg.password_env or "", "") or None)
        if self.cfg.tls:
            ctx = ssl.create_default_context(cafile=str(self._ca_file) if self._ca_file else None)
            client.tls_set_context(ctx)

        def on_connect(_c: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
            self._connected = not reason.is_failure
            self.last_error = None if self._connected else f"connect: {reason}"

        def on_disconnect(_c: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
            self._connected = False
            self.last_error = f"disconnected: {reason}"

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(1, 60)
        client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        client.loop_start()
        self._client = client

    async def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        info = self._client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=self.cfg.qos)
        if info.rc == self._mqtt.MQTT_ERR_SUCCESS:
            self.delivered += 1
        elif info.rc == self._mqtt.MQTT_ERR_QUEUE_SIZE:
            self.dropped += 1
        else:
            self.failed += 1

    async def publish_readings(self, readings: Sequence[TagReading]) -> None:
        for r in readings:
            self._publish(f"{self.cfg.topic_prefix}/{r.plant_id}/{r.device_id}/{r.tag_id}/reading", r.to_dict())

    async def publish_events(self, events: Sequence[TagEvent]) -> None:
        for e in events:
            self._publish(f"{self.cfg.topic_prefix}/{e.plant_id}/{e.device_id}/events", e.to_dict())

    def health(self) -> AdapterHealth:
        return AdapterHealth(self.name, self.type, ok=self._connected, delivered=self.delivered, failed=self.failed,
                             dropped=self.dropped, last_error=self.last_error,
                             extra={"broker": f"{self.cfg.host}:{self.cfg.port}"})
