"""Render load-test JSON results as a Markdown performance report.

    python perf/report.py perf/results/*.json > docs/PERFORMANCE_RESULTS.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DESCRIPTIONS = {
    "A": "4,000 tags, each sampled once per minute (4,000 samples/min)",
    "B": "4,000 samples/min across schedules: 50 tags @ 1 s + 1,000 tags @ 60 s",
    "C": "4,000 tags each sampled every second (240,000 samples/min)",
}


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def render(results: list[dict[str, Any]]) -> str:
    lines = ["# Load test results", "",
             "Measured on the host listed per run, against the bundled Modbus TCP simulator on localhost.",
             "These figures describe this host and setup only; they are not a guarantee for other hardware or",
             "for real PLC response times. Re-run `perf/load_test.py` on the target industrial PC.", ""]
    for r in results:
        h = r["host"]
        lines += [
            f"## Workload {r['workload']} — {DESCRIPTIONS[r['workload']]}", "",
            f"Host: {h['platform']} {h.get('machine') or ''}, {h['cpu_count']} CPUs, Python {h['python']}; "
            f"{r['devices']} Modbus devices; database {'MySQL' if r['database'] else 'disabled (spool only)'}; "
            f"duration {r['duration_s']:,.0f} s", "",
            "| Metric | Value |", "|---|---|",
            f"| Expected samples/min | {_fmt(r['expected_samples_per_min'])} |",
            f"| Measured reads/min | {_fmt(r['measured_reads_per_min'])} |",
            f"| Measured DB rows/min | {_fmt(r['measured_written_per_min'])} |",
            f"| Reads OK / failed | {_fmt(r['reads_ok'])} / {_fmt(r['reads_failed'])} |",
            f"| Records written / dropped | {_fmt(r['records_written'])} / {_fmt(r['records_dropped'])} |",
            f"| Scan overruns | {_fmt(r['scan_overruns'])} |",
            f"| Final queue / spool rows | {_fmt(r['final_queue'])} / {_fmt(r['final_spool'])} |",
            f"| Process CPU avg / max (% of one core) | {_fmt(r['cpu_percent_avg'])} / {_fmt(r['cpu_percent_max'])} |",
            f"| Process RSS max (MB) | {_fmt(r['rss_mb_max'])} |",
            f"| PLC poll duration p95 (max of samples) / max (ms) | {_fmt(r['poll_p95_ms_max'])} / "
            f"{_fmt(r['poll_max_ms'])} |",
            f"| DB batch insert p95 / max (ms) | {_fmt(r['db_insert_p95_ms_max'])} / {_fmt(r['db_insert_max_ms'])} |",
            "",
        ]
    return "\n".join(lines)


def main(paths: list[str]) -> int:
    results = [json.loads(Path(p).read_text()) for p in paths]
    print(render(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
