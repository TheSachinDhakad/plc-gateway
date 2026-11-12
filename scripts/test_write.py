"""Safe commissioning write: one audited write to one configured tag, with read-back.

    python scripts/test_write.py --device-id siemens_01 --tag-id test_write_value --value 12.5 \\
        --operator "j.smith" --reason "commissioning DB100 write path"

All gateway write gates apply exactly as in production (read_only_mode, kill switch, device
write_enabled, tag access read_write, allowed_roles, type and engineering limits, audit store
available). The command is recorded in plc_write_commands and audit_logs.

Use a dedicated scratch tag that PLC logic does not use (e.g. DB100.DBD12 TestWriteValue).
This opens its own connection to the device: for Modbus RTU (single serial port) stop the
gateway service first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config_loader import load_config  # noqa: E402
from core.tag_manager import TagManager  # noqa: E402
from core.write_manager import Principal, WriteManager  # noqa: E402
from database.mysql_client import MySQLClient, MySQLParams  # noqa: E402
from database.repository import Repository  # noqa: E402
from drivers import get_driver_class  # noqa: E402
from utils.exceptions import GatewayError, WriteRejectedError  # noqa: E402
from utils.logger import setup_logging  # noqa: E402


class DirectTarget:
    """Minimal write target bound to a freshly connected driver."""

    def __init__(self, driver: Any) -> None:
        self.driver = driver
        self.device_id = driver.device_id

    async def write(self, tag: Any, value: Any, timeout_s: float) -> Any:
        return await asyncio.wait_for(self.driver.write_tag(tag, value), timeout_s)

    async def read_now(self, tag: Any, timeout_s: float) -> Any:
        return await asyncio.wait_for(self.driver.read_tag(tag), timeout_s)


async def run(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir, env_file=args.env_file)
    setup_logging(args.config_dir / "logging.yaml", config.resolve_path(config.settings.gateway.log_dir))
    device = config.device(args.device_id)
    if device is None:
        print(f"unknown device {args.device_id}", file=sys.stderr)
        return 2
    value = json.loads(args.value)
    db = MySQLClient(MySQLParams.from_settings(config.settings.database))
    await db.start()
    repo = Repository(db, config.settings.gateway.gateway_id)
    cls = get_driver_class(device.driver)
    executor = ThreadPoolExecutor(max_workers=1) if cls.blocking else None
    driver = cls(device, executor=executor)
    try:
        await driver.register_tags([t for t in config.tags[device.device_id] if t.enabled])
        await driver.connect()
        tag = next((t for t in config.tags[device.device_id] if t.tag_id == args.tag_id), None)
        if tag is not None:
            before = await driver.read_tag(tag)
            print(f"current value: {before.value!r} ({before.quality})")
        wm = WriteManager(config, TagManager(config), repo, {device.device_id: DirectTarget(driver)})
        principal = Principal(args.operator, frozenset({args.role}))
        request_id = args.request_id or f"cli-{uuid.uuid4().hex[:16]}"
        cmd = await wm.submit(principal, device.device_id, args.tag_id, value, request_id, args.reason)
        print(json.dumps(cmd.to_dict(), indent=2, default=str))
        return 0 if cmd.status.value == "SUCCESS" else 1
    except WriteRejectedError as exc:
        print(f"REJECTED [{exc.code}]: {exc}", file=sys.stderr)
        return 3
    except GatewayError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 4
    finally:
        await driver.disconnect()
        if executor:
            executor.shutdown(wait=False)
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--tag-id", required=True)
    parser.add_argument("--value", required=True, help="JSON literal: 12.5, true, 42, \"text\", [1,2]")
    parser.add_argument("--operator", required=True, help="person performing the write (audited)")
    parser.add_argument("--role", default="operator")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--request-id", help="reuse to query a previous outcome without re-executing")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
