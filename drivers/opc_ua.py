"""OPC UA client driver (asyncua 2.1).

* Reads use the Read service for many nodes at once (``read_attributes``) and keep the
  server's StatusCode and SourceTimestamp (``timestamp_origin = "device"``).
* Security: Basic256Sha256 / Aes128_Sha256_RsaOaep / Aes256_Sha256_RsaPss with Sign or
  SignAndEncrypt. The server certificate is pinned (``server_cert``). ``None`` security is
  only possible with ``allow_insecure: true``. Deprecated policies are not offered.
* Username/password are read from environment variables named in the config.
* Writes send an explicitly typed Variant matching the configured data type.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from asyncua import Client, ua
from asyncua.crypto import security_policies as sp

from drivers.base import BaseDriver
from drivers.codecs.datatypes import DataType, python_value_matches
from models.plc import DeviceConfig, DeviceInfo, DriverHealth, OpcUaConnection
from models.reading import Quality, RawReadResult, TagValue, WriteResult
from models.tag import TagConfig
from utils.exceptions import ConfigError, DriverConnectionError, TagConfigError
from utils.timeutil import ensure_utc, utc_now

log = logging.getLogger(__name__)

VARIANT_TYPES: dict[DataType, ua.VariantType] = {
    DataType.BOOL: ua.VariantType.Boolean,
    DataType.SINT: ua.VariantType.SByte,
    DataType.INT: ua.VariantType.Int16,
    DataType.DINT: ua.VariantType.Int32,
    DataType.LINT: ua.VariantType.Int64,
    DataType.USINT: ua.VariantType.Byte,
    DataType.UINT: ua.VariantType.UInt16,
    DataType.UDINT: ua.VariantType.UInt32,
    DataType.ULINT: ua.VariantType.UInt64,
    DataType.BYTE: ua.VariantType.Byte,
    DataType.WORD: ua.VariantType.UInt16,
    DataType.DWORD: ua.VariantType.UInt32,
    DataType.LWORD: ua.VariantType.UInt64,
    DataType.REAL: ua.VariantType.Float,
    DataType.LREAL: ua.VariantType.Double,
    DataType.STRING: ua.VariantType.String,
    DataType.WSTRING: ua.VariantType.String,
}

POLICIES: dict[str, type[sp.SecurityPolicy]] = {
    "Basic256Sha256": sp.SecurityPolicyBasic256Sha256,
    "Aes128_Sha256_RsaOaep": sp.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256_Sha256_RsaPss": sp.SecurityPolicyAes256Sha256RsaPss,
}
MODES = {"Sign": ua.MessageSecurityMode.Sign, "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt}
_CONN_ERRORS = (ConnectionError, OSError, TimeoutError)
_SESSION_BAD = {"BadSessionIdInvalid", "BadSessionClosed", "BadSecureChannelClosed", "BadConnectionClosed",
                "BadServerNotConnected", "BadCommunicationError", "BadTimeout", "BadSecureChannelIdInvalid"}


class OpcUaDriver(BaseDriver):
    driver_name = "opc_ua"
    supports_write = True
    supports_batch_write = True

    def __init__(self, device: DeviceConfig, **kwargs: Any) -> None:
        super().__init__(device, **kwargs)
        assert isinstance(device.connection, OpcUaConnection)
        self.conn: OpcUaConnection = device.connection
        self._client: Client | None = None
        self._connected = False
        self._nodes: dict[str, Any] = {}

    @classmethod
    def validate_tag(cls, device: DeviceConfig, tag: TagConfig) -> None:
        if tag.data_type not in VARIANT_TYPES:
            raise TagConfigError(f"data type {tag.data_type} not supported by OPC UA driver")
        try:
            ua.NodeId.from_string(tag.address)
        except Exception as exc:
            raise TagConfigError(f"invalid NodeId {tag.address!r}: {exc}") from exc

    def _resolve(self, value: str | None) -> str | None:
        if value is None:
            return None
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        return path

    async def _configure_security(self, client: Client) -> None:
        if self.conn.security_policy == "None":
            log.warning("OPC UA connection without security (allow_insecure)",
                        extra={"device_id": self.device_id})
            return
        cert, key = self._resolve(self.conn.client_cert), self._resolve(self.conn.client_key)
        server_cert = self._resolve(self.conn.server_cert)
        for label, path in (("client_cert", cert), ("client_key", key), ("server_cert", server_cert)):
            if path is not None and not os.path.isfile(path):  # noqa: ASYNC240 - one-off at connect
                raise ConfigError(f"{self.device_id}: {label} file not found: {path}")
        key_password = os.environ.get(self.conn.client_key_password_env) if self.conn.client_key_password_env else None
        client.application_uri = self.conn.application_uri
        await client.set_security(POLICIES[self.conn.security_policy], certificate=cert, private_key=key,
                                  private_key_password=key_password, server_certificate=server_cert,
                                  mode=MODES[self.conn.security_mode])

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        await self.disconnect()
        client = Client(self.conn.endpoint, timeout=self.conn.timeout_s)
        try:
            await self._configure_security(client)
            if self.conn.username_env and self.conn.password_env:
                user = os.environ.get(self.conn.username_env)
                password = os.environ.get(self.conn.password_env)
                if not user or password is None:
                    raise ConfigError(f"{self.device_id}: OPC UA credentials env vars not set")
                client.set_user(user)
                client.set_password(password)
            await client.connect()
        except ConfigError:
            raise
        except Exception as exc:
            raise DriverConnectionError(f"{self.device_id}: OPC UA connect {self.conn.endpoint} failed: {exc}") from exc
        self._client = client
        self._connected = True
        self._nodes = {}
        log.info("OPC UA connected (%s/%s)", self.conn.security_policy, self.conn.security_mode,
                 extra={"device_id": self.device_id, "operation": "connect"})

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.debug("OPC UA disconnect error ignored: %s", exc)

    def is_connected(self) -> bool:
        return self._client is not None and self._connected

    def _require(self) -> Client:
        if self._client is None or not self._connected:
            raise DriverConnectionError(f"{self.device_id}: not connected")
        return self._client

    def _node(self, tag: TagConfig) -> Any:
        node = self._nodes.get(tag.tag_id)
        if node is None:
            node = self._require().get_node(tag.address)
            self._nodes[tag.tag_id] = node
        return node

    def _connection_failure(self, exc: BaseException) -> DriverConnectionError:
        self._connected = False
        return DriverConnectionError(f"{self.device_id}: OPC UA communication failed: {exc!r}")

    # ------------------------------------------------------------------ reads
    def _to_result(self, tag: TagConfig, dv: ua.DataValue, duration: float) -> RawReadResult:
        status = dv.StatusCode if dv.StatusCode is not None else ua.StatusCode(0)
        src: datetime = ensure_utc(dv.SourceTimestamp) if dv.SourceTimestamp else utc_now()
        origin = "device" if dv.SourceTimestamp else "gateway_read"
        if status.is_bad():
            quality = Quality.BAD_ADDRESS if status.name in ("BadNodeIdUnknown", "BadNodeIdInvalid",
                                                             "BadAttributeIdInvalid") else Quality.BAD
            return RawReadResult(tag.tag_id, None, quality, src, origin, f"OPCUA_{status.name}",
                                 status.doc, duration)
        value = dv.Value.Value if dv.Value is not None else None
        if isinstance(value, bytes | bytearray):
            value = list(value)
        ok = (isinstance(value, list) and len(value) == (tag.array_length or -1)
              and all(python_value_matches(tag.data_type, v) for v in value)) if tag.array_length else (
            python_value_matches(tag.data_type, value))
        if not ok:
            return RawReadResult(tag.tag_id, None, Quality.BAD_TYPE_MISMATCH, src, origin, "OPCUA_TYPE_MISMATCH",
                                 f"server value {type(value).__name__} does not match {tag.data_type}", duration)
        if isinstance(value, int) and tag.data_type in (DataType.REAL, DataType.LREAL):
            value = float(value)
        quality = Quality.UNCERTAIN if status.is_uncertain() else Quality.GOOD
        code = f"OPCUA_{status.name}" if quality is Quality.UNCERTAIN else None
        return RawReadResult(tag.tag_id, value, quality, src, origin, code, None, duration)

    async def read_tags(self, tags: Sequence[TagConfig]) -> list[RawReadResult]:
        client = self._require()
        out: list[RawReadResult] = []
        size = self.conn.max_nodes_per_read
        for i in range(0, len(tags), size):
            chunk = list(tags[i:i + size])
            nodes = [self._node(t) for t in chunk]
            t0 = time.perf_counter()
            try:
                values = await client.read_attributes(nodes, ua.AttributeIds.Value)
            except ua.UaStatusCodeError as exc:
                name = ua.StatusCode(exc.code).name if hasattr(exc, "code") else str(exc)
                if name in _SESSION_BAD:
                    raise self._connection_failure(exc) from exc
                out += [self.bad(t, Quality.BAD, f"OPCUA_{name}", str(exc)) for t in chunk]
                continue
            except _CONN_ERRORS as exc:
                raise self._connection_failure(exc) from exc
            duration = (time.perf_counter() - t0) * 1000
            out += [self._to_result(t, dv, duration) for t, dv in zip(chunk, values, strict=True)]
        return out

    # ------------------------------------------------------------------ writes
    def _variant(self, tag: TagConfig, value: TagValue) -> ua.DataValue:
        vt = VARIANT_TYPES[tag.data_type]
        return ua.DataValue(ua.Variant(value, vt))

    async def write_tags(self, items: Sequence[tuple[TagConfig, TagValue]]) -> list[WriteResult]:
        client = self._require()
        nodes = [self._node(t) for t, _ in items]
        dvs = [self._variant(t, v) for t, v in items]
        t0 = time.perf_counter()
        try:
            statuses = await client.write_values(nodes, dvs, raise_on_partial_error=False)
        except _CONN_ERRORS as exc:
            self._connected = False
            duration = (time.perf_counter() - t0) * 1000
            return [self.write_failed(t, "OPCUA_CONNECTION", str(exc), duration, uncertain=True) for t, _ in items]
        except ua.UaStatusCodeError as exc:
            duration = (time.perf_counter() - t0) * 1000
            return [self.write_failed(t, "OPCUA_WRITE", str(exc), duration) for t, _ in items]
        duration = (time.perf_counter() - t0) * 1000
        out = []
        for (tag, _), status in zip(items, statuses, strict=True):
            if status.is_good():
                out.append(self.write_ok(tag, duration))
            else:
                out.append(self.write_failed(tag, f"OPCUA_{status.name}", status.doc or status.name, duration))
        return out

    async def write_tag(self, tag: TagConfig, value: TagValue) -> WriteResult:
        return (await self.write_tags([(tag, value)]))[0]

    # ------------------------------------------------------------------ info / health / discovery
    async def get_device_info(self) -> DeviceInfo:
        client = self._require()
        info = DeviceInfo(self.device_id, self.driver_name, extra={"endpoint": self.conn.endpoint})
        try:
            build = client.get_node(ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo))
            values = await client.read_attributes([build], ua.AttributeIds.Value)
            bi = values[0].Value.Value if values and values[0].Value else None
            if bi is not None:
                info.vendor = getattr(bi, "ManufacturerName", None)
                info.model = getattr(bi, "ProductName", None)
                info.firmware = getattr(bi, "SoftwareVersion", None)
        except Exception as exc:
            info.extra["build_info_error"] = str(exc)
        try:
            info.extra["namespaces"] = await client.get_namespace_array()
        except Exception as exc:
            info.extra["namespace_error"] = str(exc)
        return info

    async def health_check(self) -> DriverHealth:
        t0 = time.perf_counter()
        if not self.is_connected():
            return DriverHealth(self.device_id, False, False, detail="not connected", checked_at=utc_now())
        try:
            await self._require().check_connection()
            return DriverHealth(self.device_id, True, True, (time.perf_counter() - t0) * 1000, None, utc_now())
        except Exception as exc:
            self._connected = False
            return DriverHealth(self.device_id, False, False, detail=str(exc), checked_at=utc_now())

    async def browse(self, max_depth: int = 4) -> list[dict[str, Any]]:
        """Browse the Objects folder for variables (used by scripts/discover_tags.py)."""
        client = self._require()
        found: list[dict[str, Any]] = []

        async def walk(node: Any, depth: int, path: str) -> None:
            if depth > max_depth:
                return
            for child in await node.get_children():
                name = (await child.read_browse_name()).Name
                cls = await child.read_node_class()
                child_path = f"{path}/{name}"
                if cls == ua.NodeClass.Variable:
                    try:
                        vt = (await child.read_data_type_as_variant_type()).name
                    except Exception:
                        vt = "unknown"
                    found.append({"node_id": child.nodeid.to_string(), "path": child_path, "variant_type": vt})
                elif cls == ua.NodeClass.Object:
                    await walk(child, depth + 1, child_path)

        await walk(client.nodes.objects, 0, "")
        return found
