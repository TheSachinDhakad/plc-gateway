# Grafana integration

Grafana reads MySQL directly with the **read-only** user `gw_grafana` (see
`database/grants_example.sql`). Dashboards use the views `v_tag_latest`, `v_tag_history`,
`v_events`, `v_tags` and the table `gateway_health`. All times are stored in UTC.

## 1. Data source

Provisioned by `grafana/provisioning/datasources/mysql.yaml` (uid `plcgw-mysql`), or manually:
Connections → Data sources → MySQL — host `db-host:3306`, database `plc_gateway`, user
`gw_grafana`, **Session timezone `+00:00`**, max open connections 10. The password is supplied
through the Grafana environment (`GRAFANA_MYSQL_PASSWORD`), never in the YAML.

## 2. Dashboards

`grafana/dashboards/*.json` (regenerate with `python grafana/build_dashboards.py`):

| File | Content |
|---|---|
| `process.json` | Latest temperature/pressure table, temperature and pressure trends |
| `tag_history.json` | Any tag(s) by `$device`/`$tag` variables: averaged trend + raw samples |
| `health.json` | Queue, spool, dropped records, CPU, memory, DB insert p95, failed reads, poll p95 |
| `communication.json` | Current device status, connected timeline, COMM_LOST/RESTORED events |
| `events.json` | Events per hour by type, event log |

Provision with `grafana/provisioning/dashboards/plc_gateway.yaml` (mount dashboards to
`/var/lib/grafana/dashboards/plc-gateway`) or import the JSON files via the UI.

## 3. Query examples

Latest values (Format: Table):
```sql
SELECT device_id, tag_name, value_num, value_str, engineering_unit, quality, gateway_ts
FROM v_tag_latest ORDER BY device_id, tag_name;
```

Time series, averaged per Grafana interval (Format: Time series) — scales to long ranges:
```sql
SELECT $__timeGroupAlias(time, $__interval), CONCAT(device_id, '/', tag_id) AS metric, AVG(value_num) AS value
FROM v_tag_history
WHERE $__timeFilter(time) AND device_id = 'siemens_01' AND tag_id IN ('temperature', 'pressure') AND quality = 0
GROUP BY 1, 2 ORDER BY 1;
```

Raw samples for a short range:
```sql
SELECT time, value_num AS Temperature FROM v_tag_history
WHERE $__timeFilter(time) AND device_id = 'siemens_01' AND tag_id = 'temperature' AND quality = 0
ORDER BY time;
```

Device connectivity:
```sql
SELECT snapshot_ts AS time, device_id AS metric, connected AS value FROM gateway_health
WHERE $__timeFilter(snapshot_ts) AND device_id <> '_gateway' ORDER BY 1;
```

Events:
```sql
SELECT time, device_id, tag_id, event_type, previous_value, new_value, message
FROM v_events WHERE $__timeFilter(time) ORDER BY time DESC LIMIT 1000;
```

## 4. Performance notes

- Always filter by `device_id` and `tag_id` and use `$__timeFilter`: MySQL then uses the history
  primary key `(tag_pk, gateway_ts)` and prunes partitions (verified with `EXPLAIN`).
- Use `$__timeGroupAlias` + `AVG` for ranges longer than a few hours instead of raw rows.
- `quality = 0` is GOOD (`quality_codes` table maps the codes).
