"""industrial-plc-gateway entry point.

    python main.py                         # run with ./config and ./.env
    python main.py --config-dir config/sim # run against local simulators
    python main.py --validate-only         # validate configuration and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from core.config_loader import load_config, load_env
from services.gateway_service import GatewayService
from utils.exceptions import ConfigError
from utils.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parent
log = logging.getLogger("gateway")


async def run_gateway(config_dir: Path, env_file: Path | None, stop: asyncio.Event,
                      *, json_logs: bool = False) -> int:
    """Run until ``stop`` is set. Used by the CLI and by the Windows service host."""
    load_env(env_file)
    config = load_config(config_dir, env_file=env_file)
    s = config.settings
    setup_logging(config_dir / "logging.yaml", config.resolve_path(s.gateway.log_dir), json_console=json_logs)
    service = GatewayService(config)
    try:
        await service.start()
        await stop.wait()
    finally:
        await service.stop()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: no loop signal handlers; Ctrl+C / Ctrl+Break handled via signal.signal
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _main_async(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    return await run_gateway(args.config_dir, args.env_file, stop, json_logs=args.json_logs)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plc-gateway", description="Industrial PLC data gateway")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--validate-only", action="store_true", help="validate configuration and exit")
    parser.add_argument("--json-logs", action="store_true", help="JSON log lines on the console")
    args = parser.parse_args(argv)

    try:
        if args.validate_only:
            setup_logging(None, PROJECT_ROOT / "logs")
            config = load_config(args.config_dir, env_file=args.env_file)
            devices = config.devices(enabled_only=True)
            tags = sum(1 for d in devices for t in config.tags[d.device_id] if t.enabled)
            print(f"configuration OK: {len(devices)} enabled device(s), {tags} enabled tag(s)")
            return 0
        return asyncio.run(_main_async(args))
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(cli())
