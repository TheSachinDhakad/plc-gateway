"""Load test: runs the real GatewayService against a Modbus TCP simulator (separate process).

Workloads (do not confuse them):

  A  4,000 tags, each sampled once per minute                    ->   4,000 samples/min
  B  4,000 samples/min across schedules: 50 tags @ 1 s (3,000/min)
     + 1,000 tags @ 60 s (1,000/min)                              ->   4,000 samples/min
  C  4,000 tags each sampled every second                         -> 240,000 samples/min

    python perf/load_test.py --workload B --duration 900              # 15 min, MySQL from env/.env
    python perf/load_test.py --workload C --duration 120 --no-db      # pipeline + spool only

Measures (sampled every 5 s): process CPU %, RSS, PLC read duration, DB insert duration,
queue size, spool rows, throughput, failed and dropped records, scan overruns. Results:
perf/results/<timestamp>_<workload>.json and a Markdown report (perf/report.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config_loader import load_config  # noqa: E402
from services.gateway_service import GatewayService  # noqa: E402
from utils.logger import setup_logging  # noqa: E402

DEVICES = 4            # Modbus unit ids 1..4 on one simulator, one TCP connection per device


def workload_tags(name: str) -> list[tuple[int, int]]:
    """Return [(tag_count, poll_interval_ms)]."""
    return {"A": [(4000, 60_000)], "B": [(50, 1000), (1000, 60_000)], "C": [(4000, 1000)]}[name]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_config(tmp: Path, workload: str, port: int, database: bool) -> Path:
    cfg = tmp / "config"
    cfg.mkdir(parents=True)
    shutil.copy(ROOT / "config" / "logging.yaml", cfg / "logging.yaml")
    settings = {
        "gateway": {"gateway_id": f"gw_load_{workload.lower()}", "environment": "development",
                    "status_file": str(tmp / "status.json"), "read_only_mode": True,
                    "write_kill_switch_file": str(tmp / "WRITE_DISABLE"), "log_dir": str(tmp / "logs")},
        "buffer": {"queue_max_size": 500_000, "durability_mode": "spool_first",
                   "spool_path": str(tmp / "spool.sqlite3"), "flush_interval_ms": 1000, "batch_size": 5000},
        "database": {"enabled": database, "pool_min": 1, "pool_max": 4},
        "health": {"snapshot_interval_s": 30},
    }
    (cfg / "settings.yaml").write_text(yaml.safe_dump(settings))
    groups = workload_tags(workload)
    per_device: list[list[dict[str, Any]]] = [[] for _ in range(DEVICES)]
    n = 0
    for count, interval in groups:
        for _ in range(count):
            dev = n % DEVICES
            offset = (n // DEVICES) * 2           # REAL = 2 registers, contiguous per device
            per_device[dev].append({"tag_id": f"t{n:05d}", "device_id": f"mb{dev + 1}", "name": f"Tag {n}",
                                    "address": f"holding:{offset}", "data_type": "REAL",
                                    "poll_interval_ms": interval, "logging_mode": "cyclic"})
            n += 1
    devices = []
    for i in range(DEVICES):
        (cfg / f"tags_mb{i + 1}.yaml").write_text(yaml.safe_dump({"tags": per_device[i]}))
        devices.append({"device_id": f"mb{i + 1}", "name": f"Load Modbus {i + 1}", "driver": "modbus_tcp",
                        "tags_file": f"tags_mb{i + 1}.yaml",
                        "connection": {"host": "127.0.0.1", "port": port, "unit_id": i + 1, "timeout_s": 3}})
    (cfg / "plants.yaml").write_text(yaml.safe_dump({"plants": [{"plant_id": "plant_load", "name": "Load",
                                                                 "devices": devices}]}))
    return cfg


async def sample(gw: GatewayService, proc: psutil.Process) -> dict[str, Any]:
    m = gw.metrics
    devices = [f"mb{i + 1}" for i in range(DEVICES)]
    poll = [m.duration("poll_duration_ms", device=d) for d in devices]
    return {
        "t": time.time(),
        "cpu_percent": proc.cpu_percent(None),
        "rss_mb": round(proc.memory_info().rss / 1048576, 1),
        "reads_ok": sum(m.counter("reads_ok_total", device=d) for d in devices),
        "reads_failed": sum(m.counter("reads_failed_total", device=d) for d in devices),
        "overruns": sum(m.counter("scan_overruns_total", device=d) for d in devices),
        "written": m.counter("records_written_total"),
        "dropped": m.counter_total("records_dropped_total"),
        "queue": gw.buffer.queue_size if gw.buffer else 0,
        "spool": gw.buffer.spool_rows if gw.buffer else 0,
        "poll_p95_ms": max(p["p95"] for p in poll),
        "poll_max_ms": max(p["max"] for p in poll),
        "db_p95_ms": m.duration("db_insert_duration_ms")["p95"],
        "db_max_ms": m.duration("db_insert_duration_ms")["max"],
        "spool_append_p95_ms": m.duration("spool_append_ms")["p95"],
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="plcgw_load_"))
    port = free_port()
    sim = subprocess.Popen([sys.executable,  # noqa: ASYNC220 - harness process, started once "-m", "simulators", "--no-s7", "--no-mc", "--no-opcua",
                            "--modbus-port", str(port), "--modbus-units", ",".join(str(i + 1) for i in range(DEVICES))],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(1.5)
        cfg_dir = build_config(tmp, args.workload, port, not args.no_db)
        config = load_config(cfg_dir, env_file=ROOT / ".env")
        setup_logging(cfg_dir / "logging.yaml", tmp / "logs")
        if not args.no_db:
            from database.mysql_client import MySQLClient, MySQLParams
            from scripts.init_database import create_partitions, migrate

            db = MySQLClient(MySQLParams.from_settings(config.settings.database))
            await db.start()
            await migrate(db, verbose=False)
            await create_partitions(db, 2, config.settings.gateway.gateway_id)
            await db.close()
        gw = GatewayService(config)
        proc = psutil.Process()
        proc.cpu_percent(None)
        await gw.start()
        samples = []
        start = time.time()
        while time.time() - start < args.duration:
            await asyncio.sleep(5)
            s = await sample(gw, proc)
            samples.append(s)
            print(f"[{s['t'] - start:6.0f}s] reads={s['reads_ok']:>9,} written={s['written']:>9,} "
                  f"queue={s['queue']:>6} spool={s['spool']:>7} cpu={s['cpu_percent']:5.1f}% rss={s['rss_mb']}MB "
                  f"poll_p95={s['poll_p95_ms']:.1f}ms db_p95={s['db_p95_ms']:.1f}ms dropped={s['dropped']}",
                  flush=True)
        elapsed = time.time() - start
        # drain: give the writer time to flush what was read
        drain_start = time.time()
        while gw.buffer and (gw.buffer.queue_size or gw.buffer.spool_rows) and time.time() - drain_start < 60:  # noqa: ASYNC110
            await asyncio.sleep(1)
        final = await sample(gw, proc)
        await gw.stop()
    finally:
        sim.terminate()
        sim.wait(10)
    expected_per_min = sum(c * 60_000 / i for c, i in workload_tags(args.workload))
    cpu = [s["cpu_percent"] for s in samples[1:]] or [0.0]
    rss = [s["rss_mb"] for s in samples] or [0.0]
    result = {
        "workload": args.workload,
        "workload_definition": workload_tags(args.workload),
        "devices": DEVICES,
        "database": not args.no_db,
        "duration_s": round(elapsed, 1),
        "expected_samples_per_min": expected_per_min,
        "measured_reads_per_min": round(final["reads_ok"] / elapsed * 60, 1),
        "measured_written_per_min": round(final["written"] / elapsed * 60, 1) if not args.no_db else None,
        "reads_ok": final["reads_ok"],
        "reads_failed": final["reads_failed"],
        "records_written": final["written"],
        "records_dropped": final["dropped"],
        "scan_overruns": final["overruns"],
        "final_queue": final["queue"],
        "final_spool": final["spool"],
        "cpu_percent_avg": round(sum(cpu) / len(cpu), 2),
        "cpu_percent_max": round(max(cpu), 2),
        "rss_mb_max": max(rss),
        "poll_p95_ms_max": max(s["poll_p95_ms"] for s in samples) if samples else None,
        "poll_max_ms": final["poll_max_ms"],
        "db_insert_p95_ms_max": max(s["db_p95_ms"] for s in samples) if samples else None,
        "db_insert_max_ms": final["db_max_ms"],
        "host": {"platform": sys.platform, "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
                 "machine": os.uname().machine if hasattr(os, "uname") else None},
        "samples": samples,
    }
    shutil.rmtree(tmp, ignore_errors=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workload", choices=["A", "B", "C"], required=True)
    parser.add_argument("--duration", type=int, default=900, help="seconds (spec: >= 900)")
    parser.add_argument("--no-db", action="store_true", help="measure acquisition + spool only")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    out_dir = ROOT / "perf" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d_%H%M%S')}_{args.workload}{'_nodb' if args.no_db else ''}"
    path = out_dir / f"{stem}.json"
    path.write_text(json.dumps(result, indent=2))
    from perf.report import render

    (out_dir / f"{stem}.md").write_text(render([result]))
    print(f"\nresult: {path}")
    print(render([result]))
    return 0 if result["records_dropped"] == 0 and result["reads_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
