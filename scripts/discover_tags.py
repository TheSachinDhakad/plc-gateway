"""Generate tag YAML from a live source or a Kepware export.

    # OPC UA: browse the address space (variables only)
    python scripts/discover_tags.py opcua --device-id opcua_01 --endpoint opc.tcp://10.0.0.5:4840 --insecure
    # Rockwell Logix: read the controller tag list
    python scripts/discover_tags.py logix --device-id logix_01 --path 10.0.0.20/0
    # Kepware CSV tag export -> gateway YAML (addresses validated against the chosen driver)
    python scripts/discover_tags.py kepware-csv --device-id siemens_01 --driver siemens_s7 --csv export.csv

Output goes to stdout (or --out). Every generated tag is enabled: false and must be reviewed.
Siemens S7 (classic) has no symbol access, so S7 tags cannot be discovered from the PLC —
export them from TIA Portal or Kepware instead.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drivers import get_driver_class  # noqa: E402
from models.plc import DeviceConfig  # noqa: E402
from models.tag import TagConfig  # noqa: E402
from utils.exceptions import GatewayError  # noqa: E402

KEPWARE_TYPES = {
    "boolean": "BOOL", "char": "SINT", "byte": "BYTE", "short": "INT", "word": "WORD", "long": "DINT",
    "dword": "DWORD", "float": "REAL", "double": "LREAL", "string": "STRING", "llong": "LINT", "qword": "LWORD",
    "bcd": None, "lbcd": None, "date": None,
}
UA_TYPES = {"Boolean": "BOOL", "SByte": "SINT", "Byte": "USINT", "Int16": "INT", "UInt16": "UINT",
            "Int32": "DINT", "UInt32": "UDINT", "Int64": "LINT", "UInt64": "ULINT", "Float": "REAL",
            "Double": "LREAL", "String": "STRING"}
LOGIX_TYPES = {"BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL", "STRING",
               "DWORD"}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return (s or "tag")[:64]


def unique(tag_id: str, seen: set[str]) -> str:
    base, n = tag_id[:60], 1
    while tag_id in seen:
        n += 1
        tag_id = f"{base}_{n}"
    seen.add(tag_id)
    return tag_id


def entry(device_id: str, tag_id: str, name: str, address: str, data_type: str, *, poll_ms: int = 1000,
          array_length: int | None = None, note: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"tag_id": tag_id, "device_id": device_id, "name": name[:128], "address": address,
                           "data_type": data_type, "poll_interval_ms": poll_ms, "enabled": False,
                           "logging_mode": "cyclic"}
    if array_length:
        out["array_length"] = array_length
    if note:
        out["description"] = note[:512]
    return out


async def from_opcua(args: argparse.Namespace) -> list[dict[str, Any]]:
    from drivers.opc_ua import OpcUaDriver

    conn: dict[str, Any] = {"endpoint": args.endpoint}
    if args.insecure:
        conn.update(security_policy="None", security_mode="None", allow_insecure=True)
    else:
        conn.update(client_cert=args.client_cert, client_key=args.client_key, server_cert=args.server_cert)
    device = DeviceConfig.model_validate({"device_id": args.device_id, "name": "discovery", "driver": "opc_ua",
                                         "connection": conn})
    drv = OpcUaDriver(device)
    await drv.connect()
    try:
        nodes = await drv.browse(max_depth=args.depth)
    finally:
        await drv.disconnect()
    seen: set[str] = set()
    out = []
    for n in nodes:
        dt = UA_TYPES.get(n["variant_type"])
        if dt is None:
            continue
        out.append(entry(args.device_id, unique(slug(n["path"]), seen), n["path"].lstrip("/"), n["node_id"], dt))
    return out


def from_logix(args: argparse.Namespace) -> list[dict[str, Any]]:
    from pycomm3 import LogixDriver

    seen: set[str] = set()
    out = []
    with LogixDriver(args.path, init_program_tags=True) as plc:
        for tag in plc.get_tag_list(program="*"):
            dt = str(tag.get("data_type_name") or "")
            if dt not in LOGIX_TYPES:
                continue
            name = tag["tag_name"]
            dims = [d for d in (tag.get("dimensions") or []) if d]
            out.append(entry(args.device_id, unique(slug(name), seen), name,
                             f"{name}[0]" if dims else name, dt, array_length=dims[0] if dims else None))
    return out


def from_kepware(args: argparse.Namespace) -> list[dict[str, Any]]:
    device = DeviceConfig.model_validate({"device_id": args.device_id, "name": "import", "driver": args.driver,
                                         "connection": _placeholder_connection(args.driver)})
    driver_cls = get_driver_class(args.driver)
    seen: set[str] = set()
    out = []
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("Tag Name") or "").strip()
            address = (row.get("Address") or "").strip()
            ktype = (row.get("Data Type") or "").strip().lower()
            if not name or not address:
                continue
            dt = KEPWARE_TYPES.get(ktype)
            scan = int(float(row.get("Scan Rate") or 1000))
            tag_id = unique(slug(name), seen)
            if dt is None:
                out.append(entry(args.device_id, tag_id, name, address, "DINT", poll_ms=max(scan, 100),
                                 note=f"REVIEW: Kepware type '{ktype}' has no direct equivalent"))
                continue
            item = entry(args.device_id, tag_id, name, address, dt, poll_ms=max(scan, 100))
            try:
                driver_cls.validate_tag(device, TagConfig.model_validate(item))
            except (GatewayError, ValueError) as exc:
                item["description"] = f"REVIEW: address not valid for {args.driver}: {exc}"[:512]
            out.append(item)
    return out


def _placeholder_connection(driver: str) -> dict[str, Any]:
    return {
        "siemens_s7": {"host": "0.0.0.0"},
        "rockwell_logix": {"path": "0.0.0.0/0"},
        "mitsubishi_mc": {"host": "0.0.0.0", "port": 5000, "plc_series": "FX5U"},
        "modbus_tcp": {"host": "0.0.0.0"},
        "modbus_rtu": {"serial_port": "COM1"},
    }[driver]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("opcua")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--insecure", action="store_true", help="security None (lab use only)")
    p.add_argument("--client-cert")
    p.add_argument("--client-key")
    p.add_argument("--server-cert")
    p.add_argument("--depth", type=int, default=4)
    p = sub.add_parser("logix")
    p.add_argument("--path", required=True)
    p = sub.add_parser("kepware-csv")
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--driver", required=True,
                   choices=["siemens_s7", "rockwell_logix", "mitsubishi_mc", "modbus_tcp", "modbus_rtu"])
    for sp in sub.choices.values():
        sp.add_argument("--device-id", required=True)
        sp.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.mode == "opcua":
        tags = asyncio.run(from_opcua(args))
    elif args.mode == "logix":
        tags = from_logix(args)
    else:
        tags = from_kepware(args)
    text = "# GENERATED by scripts/discover_tags.py — review every tag, then set enabled: true\n" + yaml.safe_dump(
        {"tags": tags}, sort_keys=False, allow_unicode=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {len(tags)} tags to {args.out}")
    else:
        print(text)
    review = sum(1 for t in tags if str(t.get("description", "")).startswith("REVIEW"))
    if review:
        print(f"{review} tag(s) need review (description starts with REVIEW)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
