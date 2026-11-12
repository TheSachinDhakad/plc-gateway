"""Apply database migrations and create initial partitions.

    python scripts/init_database.py                       # uses MYSQL_* from .env / environment
    python scripts/init_database.py --dsn mysql://user:pw@127.0.0.1:3306/plc_gateway

The MySQL user needs CREATE/ALTER/INDEX/CREATE VIEW/INSERT on the target schema for this step
(run with an administrative account; the gateway itself runs with the least-privilege user
from database/grants_example.sql). Idempotent: safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config_loader import load_config, load_env  # noqa: E402
from database.mysql_client import MySQLClient, MySQLParams  # noqa: E402
from database.repository import Repository  # noqa: E402
from utils.timeutil import utc_now  # noqa: E402

MIGRATIONS = ROOT / "database" / "migrations"


def split_statements(sql: str) -> list[str]:
    """Split a migration file on ';' at end of line. Migrations contain no procedures/triggers."""
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in re.split(r";\s*$", "\n".join(lines), flags=re.M) if s.strip()]


async def applied_versions(db: MySQLClient) -> set[int]:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'schema_migrations'")
    if not row or not row["n"]:
        return set()
    return {int(r["version"]) for r in await db.fetch_all("SELECT version FROM schema_migrations")}


async def migrate(db: MySQLClient, *, verbose: bool = True) -> list[str]:
    done = await applied_versions(db)
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in done:
            continue
        for stmt in split_statements(path.read_text(encoding="utf-8")):
            await db.execute(stmt)
        applied.append(path.name)
        if verbose:
            print(f"applied {path.name}")
    return applied


async def create_partitions(db: MySQLClient, days_ahead: int, gateway_id: str) -> list[str]:
    repo = Repository(db, gateway_id)
    today = utc_now().date()
    created = await repo.ensure_partitions("plc_tag_history", start=today - timedelta(days=1),
                                           until=today + timedelta(days=days_ahead), monthly=False)
    created += await repo.ensure_partitions("plc_events", start=today.replace(day=1),
                                            until=today + timedelta(days=62), monthly=True)
    return created


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--dsn", help="mysql://user:password@host:port/database (overrides env)")
    args = parser.parse_args()
    load_env(args.env_file)
    config = load_config(args.config_dir, env_file=args.env_file, check_drivers=False)
    params = MySQLParams.from_dsn(args.dsn) if args.dsn else MySQLParams.from_settings(config.settings.database)
    db = MySQLClient(params)
    await db.start()
    try:
        applied = await migrate(db)
        if not applied:
            print("schema up to date")
        created = await create_partitions(db, config.settings.retention.partitions_ahead_days,
                                          config.settings.gateway.gateway_id)
        print(f"partitions created: {', '.join(created) if created else 'none needed'}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
