"""OPC DA COM bridge — runs as a separate Windows process (``python -m drivers.opc_da_bridge``).

Why a subprocess: OPC DA is COM/DCOM. COM apartment threading, 32-bit-only automation
wrappers and DCOM hangs must not destabilise the gateway process. The bridge can be started
with a different (e.g. 32-bit) interpreter via ``connection.bridge_python``.

Requirements: Windows, pywin32, and a registered OPC DA Automation 2.0 wrapper
(ProgID ``OPC.Automation`` — e.g. the OPC Foundation / Graybox wrapper ``OPCDAAuto.dll``).

Protocol: one JSON object per line on stdin; one JSON response per line on stdout::

    {"id": 1, "op": "connect", "server": "Kepware.KEPServerEX.V6", "host": "localhost", "automation": "OPC.Automation"}
    {"id": 2, "op": "read", "items": ["Channel1.Device1.Tag1", ...]}
    {"id": 3, "op": "write", "item": "Channel1.Device1.Tag1", "value": 12.5}
    {"id": 4, "op": "info"}
    {"id": 5, "op": "disconnect"}

Responses: {"id": n, "ok": true, "result": ...} or {"id": n, "ok": false, "error": "..."}.
Read results: [{"item", "value", "quality" (int OPC quality), "timestamp" (ISO UTC), "error"}].

Hardware/software validation required: behaviour depends on the installed wrapper and OPC
server. Reads are performed per item with ``OPCItem.Read(OPC_DS_DEVICE)``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

OPC_DS_DEVICE = 2


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    try:
        dt = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                      getattr(value, "microsecond", 0), tzinfo=UTC)
        return dt.isoformat()
    except Exception:
        return None


def _plain(value: Any) -> Any:
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return str(value)


class Bridge:
    def __init__(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._win32 = win32com.client
        self._server: Any = None
        self._group: Any = None
        self._items: dict[str, Any] = {}
        self._handle = 0

    def connect(self, server: str, host: str, automation: str) -> dict[str, Any]:
        self._server = self._win32.Dispatch(automation)
        if host in ("", "localhost", "127.0.0.1"):
            self._server.Connect(server)
        else:
            self._server.Connect(server, host)
        groups = self._server.OPCGroups
        groups.DefaultGroupIsActive = True
        groups.DefaultGroupUpdateRate = 1000
        self._group = groups.Add("gateway")
        self._group.IsSubscribed = False
        self._items = {}
        return {"server_state": int(self._server.ServerState)}

    def _item(self, item_id: str) -> Any:
        item = self._items.get(item_id)
        if item is None:
            self._handle += 1
            item = self._group.OPCItems.AddItem(item_id, self._handle)
            self._items[item_id] = item
        return item

    def read(self, items: list[str]) -> list[dict[str, Any]]:
        out = []
        for item_id in items:
            try:
                value, quality, ts = self._item(item_id).Read(OPC_DS_DEVICE)
                out.append({"item": item_id, "value": _plain(value), "quality": int(quality),
                            "timestamp": _ts(ts), "error": None})
            except Exception as exc:
                out.append({"item": item_id, "value": None, "quality": 0, "timestamp": None, "error": str(exc)})
        return out

    def write(self, item_id: str, value: Any) -> None:
        self._item(item_id).Write(value)

    def info(self) -> dict[str, Any]:
        s = self._server
        return {"vendor": str(s.VendorInfo), "version": f"{s.MajorVersion}.{s.MinorVersion}.{s.BuildNumber}",
                "server_name": str(s.ServerName), "server_state": int(s.ServerState)}

    def disconnect(self) -> None:
        if self._server is not None:
            try:
                self._server.OPCGroups.RemoveAll()
                self._server.Disconnect()
            finally:
                self._server = None
                self._items = {}


def main() -> int:
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"id": 0, "ok": False, "error": "OPC DA bridge requires Windows"}) + "\n")
        return 2
    bridge = Bridge()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        op = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            op = req["op"]
            if op == "connect":
                result: Any = bridge.connect(req["server"], req.get("host", "localhost"),
                                             req.get("automation", "OPC.Automation"))
            elif op == "read":
                result = bridge.read(list(req["items"]))
            elif op == "write":
                bridge.write(req["item"], req["value"])
                result = None
            elif op == "info":
                result = bridge.info()
            elif op == "disconnect":
                bridge.disconnect()
                result = None
            else:
                raise ValueError(f"unknown op {op}")
            resp = {"id": req_id, "ok": True, "result": result}
        except Exception as exc:
            resp = {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
        if op == "disconnect":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
