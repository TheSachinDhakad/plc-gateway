"""Generate Grafana dashboard JSON (schemaVersion 39, Grafana 10/11) for the MySQL datasource.

    python grafana/build_dashboards.py      # writes grafana/dashboards/*.json

Dashboards query the read-only views (v_tag_history, v_tag_latest, v_events) and gateway_health.
Datasource uid: plcgw-mysql (see grafana/provisioning/datasources/mysql.yaml).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "mysql", "uid": "plcgw-mysql"}


def target(sql: str, fmt: str = "time_series", ref: str = "A") -> dict[str, Any]:
    return {"datasource": DS, "refId": ref, "format": fmt, "rawQuery": True, "editorMode": "code", "rawSql": sql}


def panel(pid: int, title: str, ptype: str, targets: list[dict[str, Any]], x: int, y: int, w: int, h: int,
          **extra: Any) -> dict[str, Any]:
    p = {"id": pid, "title": title, "type": ptype, "datasource": DS, "targets": targets,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}}
    p.update(extra)
    return p


def var_query(name: str, sql: str, *, multi: bool = True, include_all: bool = True) -> dict[str, Any]:
    return {"name": name, "label": name.replace("_", " ").title(), "type": "query", "datasource": DS,
            "query": sql, "definition": sql, "refresh": 1, "multi": multi, "includeAll": include_all,
            "current": {}, "sort": 1}


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], variables: list[dict[str, Any]] | None = None,
              time_from: str = "now-6h", refresh: str = "30s") -> dict[str, Any]:
    return {"uid": uid, "title": title, "tags": ["plc-gateway"], "timezone": "browser", "schemaVersion": 39,
            "version": 1, "editable": True, "refresh": refresh, "time": {"from": time_from, "to": "now"},
            "templating": {"list": variables or []}, "panels": panels, "annotations": {"list": []}}


DEVICE_VAR = var_query("device", "SELECT DISTINCT device_id FROM v_tags ORDER BY 1")
TAG_VAR = var_query("tag", "SELECT tag_id FROM v_tags WHERE device_id IN (${device:sqlstring}) ORDER BY 1")

# Raw rows for short ranges; Grafana's $__timeGroupAlias downsamples long ranges server side.
HISTORY_SQL = (
    "SELECT $__timeGroupAlias(time, $__interval), CONCAT(device_id, '/', tag_id) AS metric, AVG(value_num) AS value\n"
    "FROM v_tag_history\n"
    "WHERE $__timeFilter(time) AND device_id IN (${device:sqlstring}) AND tag_id IN (${tag:sqlstring})"
    " AND quality = 0\n"
    "GROUP BY 1, 2\nORDER BY 1"
)


def process_dashboard() -> dict[str, Any]:
    def series(pid: int, title: str, tag_like: str, unit: str, y: int) -> dict[str, Any]:
        sql = (
            "SELECT $__timeGroupAlias(time, $__interval), CONCAT(device_id, '/', tag_name) AS metric, "
            "AVG(value_num) AS value\nFROM v_tag_history\n"
            f"WHERE $__timeFilter(time) AND tag_id LIKE '{tag_like}' AND quality = 0\n"
            "GROUP BY 1, 2\nORDER BY 1")
        return panel(pid, title, "timeseries", [target(sql)], 0, y, 24, 9,
                     fieldConfig={"defaults": {"unit": unit, "custom": {"lineWidth": 1, "spanNulls": False}},
                                  "overrides": []})
    latest_sql = ("SELECT device_id AS Device, tag_name AS Tag, value_num AS Value, engineering_unit AS Unit, "
                  "quality AS Quality, gateway_ts AS Updated\nFROM v_tag_latest\n"
                  "WHERE tag_id LIKE '%temp%' OR tag_id LIKE '%press%'\nORDER BY device_id, tag_name")
    return dashboard("plcgw-process", "PLC Gateway - Temperature & Pressure", [
        panel(1, "Latest values", "table", [target(latest_sql, "table")], 0, 0, 24, 7),
        series(2, "Temperature", "%temp%", "celsius", 7),
        series(3, "Pressure", "%press%", "pressurebar", 16),
    ])


def history_dashboard() -> dict[str, Any]:
    raw_sql = ("SELECT time, device_id, tag_id, value_num, value_str, quality\nFROM v_tag_history\n"
               "WHERE $__timeFilter(time) AND device_id IN (${device:sqlstring}) AND tag_id IN (${tag:sqlstring})\n"
               "ORDER BY time DESC\nLIMIT 5000")
    return dashboard("plcgw-history", "PLC Gateway - Tag History", [
        panel(1, "Tag history (averaged per interval, GOOD quality only)", "timeseries", [target(HISTORY_SQL)],
              0, 0, 24, 12),
        panel(2, "Raw samples (latest 5000 in range)", "table", [target(raw_sql, "table")], 0, 12, 24, 10),
    ], [DEVICE_VAR, TAG_VAR], time_from="now-1h")


def health_dashboard() -> dict[str, Any]:
    gw = "FROM gateway_health WHERE $__timeFilter(snapshot_ts) AND device_id = '_gateway'"
    q = lambda col, alias: target(f"SELECT snapshot_ts AS time, {col} AS `{alias}`\n{gw}\nORDER BY 1")  # noqa: E731
    reads = target("SELECT snapshot_ts AS time, device_id AS metric, reads_failed AS value\nFROM gateway_health\n"
                   "WHERE $__timeFilter(snapshot_ts) AND device_id <> '_gateway'\nORDER BY 1")
    poll = target("SELECT snapshot_ts AS time, device_id AS metric, poll_p95_ms AS value\nFROM gateway_health\n"
                  "WHERE $__timeFilter(snapshot_ts) AND device_id <> '_gateway'\nORDER BY 1")
    return dashboard("plcgw-health", "PLC Gateway - Gateway & Device Health", [
        panel(1, "Queue size", "timeseries", [q("queue_size", "queue")], 0, 0, 8, 8),
        panel(2, "Spool rows (unsent to MySQL)", "timeseries", [q("spool_rows", "spool")], 8, 0, 8, 8),
        panel(3, "Dropped records (total)", "stat", [q("dropped_total", "dropped")], 16, 0, 8, 8,
              fieldConfig={"defaults": {"thresholds": {"mode": "absolute", "steps": [
                  {"color": "green", "value": None}, {"color": "red", "value": 1}]}}, "overrides": []}),
        panel(4, "Process CPU %", "timeseries", [q("cpu_percent", "cpu")], 0, 8, 8, 8),
        panel(5, "Process memory (MB)", "timeseries", [q("rss_mb", "rss")], 8, 8, 8, 8),
        panel(6, "DB insert p95 (ms)", "timeseries", [q("db_insert_p95_ms", "db p95")], 16, 8, 8, 8),
        panel(7, "Failed reads (cumulative) per device", "timeseries", [reads], 0, 16, 12, 8),
        panel(8, "Poll duration p95 (ms) per device", "timeseries", [poll], 12, 16, 12, 8),
    ], time_from="now-24h")


def comm_dashboard() -> dict[str, Any]:
    status = target(
        "SELECT h.device_id AS Device, h.status AS Status, h.connected AS Connected, h.snapshot_ts AS `As of`,\n"
        "  JSON_UNQUOTE(JSON_EXTRACT(h.detail, '$.last_error')) AS `Last error`\nFROM gateway_health h\n"
        "JOIN (SELECT device_id, MAX(snapshot_ts) AS ts FROM gateway_health WHERE device_id <> '_gateway'\n"
        "      GROUP BY device_id) m ON m.device_id = h.device_id AND m.ts = h.snapshot_ts\nORDER BY 1", "table")
    timeline = target("SELECT snapshot_ts AS time, device_id AS metric, connected AS value\nFROM gateway_health\n"
                      "WHERE $__timeFilter(snapshot_ts) AND device_id <> '_gateway'\nORDER BY 1")
    comm_events = target("SELECT time, device_id, event_type, message\nFROM v_events\n"
                         "WHERE $__timeFilter(time) AND event_type IN ('COMM_LOST', 'COMM_RESTORED')\n"
                         "ORDER BY time DESC LIMIT 500", "table")
    return dashboard("plcgw-comm", "PLC Gateway - Communication Status", [
        panel(1, "Current device status", "table", [status], 0, 0, 24, 7),
        panel(2, "Connected (1) / disconnected (0)", "state-timeline", [timeline], 0, 7, 24, 9),
        panel(3, "Communication events", "table", [comm_events], 0, 16, 24, 9),
    ], time_from="now-24h")


def events_dashboard() -> dict[str, Any]:
    by_type = target("SELECT $__timeGroupAlias(time, '1h'), event_type AS metric, COUNT(*) AS value\nFROM v_events\n"
                     "WHERE $__timeFilter(time) AND device_id IN (${device:sqlstring})\nGROUP BY 1, 2\nORDER BY 1")
    table = target("SELECT time, device_id, tag_id, event_type, previous_value, new_value, threshold, message\n"
                   "FROM v_events\nWHERE $__timeFilter(time) AND device_id IN (${device:sqlstring})\n"
                   "ORDER BY time DESC LIMIT 1000", "table")
    return dashboard("plcgw-events", "PLC Gateway - Events", [
        panel(1, "Events per hour by type", "barchart", [by_type], 0, 0, 24, 8),
        panel(2, "Event log", "table", [table], 0, 8, 24, 14),
    ], [DEVICE_VAR], time_from="now-24h")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("process.json", process_dashboard), ("tag_history.json", history_dashboard),
                        ("health.json", health_dashboard), ("communication.json", comm_dashboard),
                        ("events.json", events_dashboard)):
        (OUT / name).write_text(json.dumps(build(), indent=2) + "\n")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
