"""Validate configuration without opening any PLC connection.

    python scripts/validate_config.py                 # ./config
    python scripts/validate_config.py --config-dir config/sim --show-tags
Exit code 0 = valid, 2 = invalid (all problems listed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config_loader import load_config  # noqa: E402
from utils.exceptions import ConfigError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--show-tags", action="store_true")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config_dir, env_file=args.env_file)
    except ConfigError as exc:
        print(f"INVALID\n{exc}", file=sys.stderr)
        return 2
    s = cfg.settings
    print(f"gateway_id      : {s.gateway.gateway_id} ({s.gateway.environment})")
    print(f"read_only_mode  : {s.gateway.read_only_mode}")
    print(f"database        : {'enabled' if s.database.enabled else 'DISABLED'}; durability={s.buffer.durability_mode}")
    total_per_min = 0.0
    for plant in cfg.plants:
        print(f"\nplant {plant.plant_id} ({'enabled' if plant.enabled else 'disabled'})")
        for d in plant.devices:
            tags = [t for t in cfg.tags[d.device_id] if t.enabled]
            rate = sum(60000 / t.poll_interval_ms for t in tags) if d.enabled and plant.enabled else 0
            total_per_min += rate
            flag = "enabled " if d.enabled else "disabled"
            print(f"  [{flag}] {d.device_id:<16} {d.driver!s:<15} tags={len(tags):<5} "
                  f"samples/min={rate:,.0f} write_enabled={d.write_enabled}")
            if args.show_tags:
                for t in tags:
                    print(f"      {t.tag_id:<24} {t.address:<28} {t.data_type!s:<7} {t.poll_interval_ms:>7} ms "
                          f"{t.logging_mode!s:<6} {t.access}")
    print(f"\nconfigured acquisition load: {total_per_min:,.0f} tag samples/minute "
          f"({total_per_min / 60:,.1f}/s)")
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
