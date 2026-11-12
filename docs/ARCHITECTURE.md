# industrial-plc-gateway — Architecture

This document describes the system **as implemented**. Class names, YAML keys and table names
here match the code. Related: [COMPATIBILITY.md](COMPATIBILITY.md), [SECURITY.md](SECURITY.md),
[PLC_CONFIGURATION.md](PLC_CONFIGURATION.md), [PERFORMANCE.md](PERFORMANCE.md).

---

## 1. Scope

### In scope

1. Direct PLC communication (no Kepware / Node-RED in the data path): Siemens S7 (classic S7,
   PUT/GET), Rockwell Logix (EtherNet/IP CIP), Mitsubishi MC protocol (3E and 1E binary),
   Modbus TCP, Modbus RTU, OPC UA client, OPC DA client (Windows only, isolated process).
2. Cyclic and event-based acquisition with per-tag poll intervals.
3. One normalized reading model regardless of vendor.
4. Durable buffering (SQLite spool) and MySQL storage: history, latest, events, health, audit.
5. Grafana dashboards on MySQL; integration adapters for a central server (REST) and MQTT.
6. Controlled, audited, allowlisted PLC writes — read-only by default.

### Not in scope / cannot be fulfilled as stated

| Requirement | Status | Reason and replacement |
|---|---|---|
| REST API (FastAPI) | **Removed at project owner's request** | Health: `data/status.json` + `gateway_health` table (Grafana). Writes: audited CLI `scripts/test_write.py` and the internal `WriteManager`. An API can be added later on top of `GatewayService` without changing other modules. |
| S7CommPlus (symbolic access to optimized DBs) | **Not supported** | No maintained open implementation. Use PUT/GET + non-optimized DBs, or the CPU's OPC UA server through the `opc_ua` driver. |
| "Universal RS232/RS485" | **Not a protocol** | Supported serial protocol: Modbus RTU. Vendor serial protocols are rejected at configuration time. |
| OPC DA on macOS | **Impossible** | COM/DCOM is Windows-only; the driver refuses to load elsewhere. |
| FX3U over Ethernet without adapter | **Hardware limitation** | Needs FX3U-ENET-ADP / FX3U-ENET(-L) (MC 1E frame), or FX3U-485ADP-MB with Modbus RTU. |
| Automatic replacement of KepServer VB scripts / FTTM | **Needs inventory** | Scaling, deadband, thresholds, edge events and adapters cover common cases; each script must be reviewed. No `eval`-style script engine is provided. |
| Value timestamps from S7 / Logix / MC / Modbus | **Protocol limitation** | These protocols carry no timestamps: `source_timestamp` = response time, `timestamp_origin = "gateway_read"`. OPC UA/DA deliver device timestamps (`"device"`). |

---

## 2. System architecture

```
 config/*.yaml + .env ──► ConfigLoader (Pydantic + per-driver address validation, fails before any I/O)
                                               │
 OT NETWORK          GATEWAY PROCESS (Python 3.11/3.12, one asyncio loop) ▼
 ┌────────────┐   ┌────────────────────────────────────────────────────────────────────────────┐
 │ S7-1200/1500│◄─►│ DeviceWorker[s7]     SiemensS7Driver ── dedicated 1-thread executor        │
 │ ControlLogix│◄─►│ DeviceWorker[logix]  RockwellLogixDriver ── dedicated 1-thread executor    │
 │ FX5U / FX3U │◄─►│ DeviceWorker[mc]     MitsubishiMCDriver (native asyncio, own framing)      │
 │ Modbus TCP  │◄─►│ DeviceWorker[mb]     ModbusTcpDriver (pymodbus asyncio)                    │
 │ RS485 RTU   │◄─►│ DeviceWorker[rtu]    ModbusRtuDriver (pymodbus asyncio serial)             │
 │ OPC UA      │◄─►│ DeviceWorker[ua]     OpcUaDriver (asyncua)                                 │
 │ OPC DA      │◄─►│ DeviceWorker[da]     OpcDaDriver ⇄ opc_da_bridge subprocess (COM, Windows) │
 └────────────┘   │   each worker: ConnectionManager (backoff+jitter, circuit breaker)          │
                  │                PollingScheduler (scan classes, drift-free, overrun-aware)   │
                  │                DataNormalizer → EventDetector                               │
                  │                        │                                                    │
                  │                        ▼  _Sink.submit(readings, events)                    │
                  │   TagManager.latest (in memory)   BufferService (bounded queue → SQLite WAL) │
                  │                                   │                    IntegrationDispatcher │
                  │                                   ▼                    ├► RestForwarder      │
                  │                     BatchWriter → Repository → MySQL   └► MqttPublisher      │
                  │   WriteManager ◄── scripts/test_write.py (gated, audited)                    │
                  │   HealthService → data/status.json + gateway_health                          │
                  │   RecoveryService → partitions ahead, retention, archive                     │
                  └────────────────────────────────────────────────────────────────────────────┘
                                   │ MySQL 8.x ─────► Grafana (read-only user, views)
```

### 2.1 Process model

- **One asyncio event loop**, one process (`main.py` / Windows service host `deploy/gateway_service.py`).
- **One `DeviceWorker` task per device** — a device's failures never block another device.
- **Blocking libraries** (python-snap7 sync client, pycomm3) run on a **dedicated single-thread
  executor per device** (`DriverManager`), serializing access to non-thread-safe clients.
- **OPC DA** runs in a **bridge subprocess** (`drivers/opc_da_bridge.py`, JSON lines over pipes) so
  COM apartment rules, 32-bit wrappers and DCOM hangs cannot destabilise the gateway.
- All driver I/O of a device (polling and writes) is serialized by `DeviceWorker.io_lock`.

### 2.2 One poll cycle

1. `PollingScheduler.next_due()` returns the scan classes (one per distinct `poll_interval_ms`) whose
   monotonic deadline has passed.
2. `driver.read_tags(scan_class.tags)` — **one batched call**: S7 contiguous-block coalescing,
   Logix multi-request packing, MC batch reads per device code, Modbus block coalescing per
   (unit, table), OPC UA `read_attributes` in chunks.
3. Per-tag failures come back as `RawReadResult` with BAD quality; a lost connection raises
   `DriverConnectionError` → `ConnectionManager.handle_failure()`.
4. `DataNormalizer` → `TagReading` (scaling, units, typing); `EventDetector` decides storage and events.
5. `TagManager.update_latest()`; `_Sink.submit()` → buffer (+ integrations). Never blocks polling.
6. `BatchWriter` drains the spool to MySQL in transactions.

---

## 3. Key design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Python **3.11** primary, 3.12 supported (`requires-python >=3.11,<3.13`). | All dependencies verified on 3.11. |
| D2 | asyncio core + per-device single-thread executors for blocking drivers. | Bounded concurrency, device isolation. |
| D3 | **Own MC protocol implementation** (`drivers/mc_protocol.py`, 3E + 1E binary). | `pymcprotocol` is unmaintained (2021) and lacks the 1E frame needed for FX3U-ENET-ADP. |
| D4 | **Own register codec** (`drivers/codecs/register_codec.py`). | pymodbus 3.16 removed `pymodbus.payload`; its converter has no byte swap. All 4 byte/word orders are tested. |
| D5 | **Sync `snap7.Client` in a per-device thread**; S7 tags coalesced into contiguous byte blocks. | Stable API; bit writes use `WordLen.Bit` (no read-modify-write). |
| D6 | **Spool-first durability** (`buffer.durability_mode: spool_first`). | Every flush interval the queue is committed to SQLite (WAL, `synchronous=FULL`) before MySQL. Loss window on power failure ≤ `flush_interval_ms`. |
| D7 | **Idempotent inserts** keyed by UUIDv7 `reading_uid` / `event_uid`; `INSERT … ON DUPLICATE KEY UPDATE`. | Replay after a crash between MySQL commit and spool delete never duplicates rows. |
| D8 | **Typed value columns** (`value_bool/int/float/str/json` + `value_type`). | No blind DOUBLE. ULINT/LWORD > 2⁶³−1 stored losslessly as text; NaN/±inf stored as text. |
| D9 | **Daily RANGE partitions** on `plc_tag_history.gateway_ts`, monthly on `plc_events.event_ts`. | O(1) retention (`DROP PARTITION`), partition pruning. Partitioned tables have no FKs (MySQL limitation). |
| D10 | Latest values **coalesced per batch** and only moved forward in time. | Replay of old spool data cannot regress `plc_tag_latest`. |
| D11 | **aiomysql** pool + parameterized SQL; **PyMySQL pinned to 1.1.2**. | PyMySQL 1.2.x breaks aiomysql 0.3.2 for bytes parameters (found by the MySQL tests). |
| D12 | **No network API** (owner decision). Writes only through `WriteManager` (CLI `scripts/test_write.py`). | Smallest attack surface on the OT host. |
| D13 | Writes: default deny + independent gates + kill switch + limits + read-back + idempotent `request_id`; uncertain outcomes never retried. | Industrial safety (§8). |
| D14 | stdlib `logging` + JSON formatter + `contextvars` + secret redaction. | Few dependencies, structured logs. |
| D15 | Pydantic v2 for configuration; slotted dataclasses on the hot path. | Validation at the edge, low overhead per reading. |
| D16 | Integration adapters see only normalized models; each has a bounded queue (REST: durable outbox). | Drivers never know about Grafana, central servers or SAP. |
| D17 | Windows service via pywin32 (`deploy/gateway_service.py`) with SCM recovery actions. | Native start/stop/restart. |
| D18 | Exact version pins; upgrade only after re-running simulation, MySQL and hardware tests. | pymodbus/python-snap7 have broken APIs between minor versions. |
| D19 | `VALUES()` in `ON DUPLICATE KEY UPDATE` kept (deprecated, still supported in 8.4 LTS). | PyMySQL `executemany` batching cannot parse the row-alias syntax; per-row inserts would be ~100× slower. Revisit with MySQL 9 / a newer client. |

---

## 4. Driver contract (`drivers/base.py`)

```python
class BaseDriver(abc.ABC):
    driver_name: ClassVar[str]; supports_write: ClassVar[bool]; supports_batch_write: ClassVar[bool]
    platforms: ClassVar[frozenset[str]]; blocking: ClassVar[bool]
    def __init__(self, device: DeviceConfig, *, executor: Executor | None = None)
    @classmethod check_platform() -> None                         # PlatformNotSupportedError
    @classmethod validate_tag(device, tag) -> None                # TagConfigError (at config load)
    async register_tags(tags) -> None
    async connect() / async disconnect() (never raises) / is_connected() -> bool
    async read_tag(tag) -> RawReadResult
    async read_tags(tags) -> list[RawReadResult]                  # same order; per-tag errors as BAD quality
    async write_tag(tag, value) -> WriteResult
    async write_tags(items) -> list[WriteResult]
    async get_device_info() -> DeviceInfo
    async health_check() -> DriverHealth
```

Rules: `read_tags` raises only `DriverConnectionError`; unsupported operations raise
`UnsupportedOperationError`; drivers never generate values; retries belong to `ConnectionManager`.

**Quality codes** (`models/reading.py: Quality`, stored as TINYINT, see table `quality_codes`):
`GOOD(0) UNCERTAIN(1) BAD(2) BAD_NOT_CONNECTED(3) BAD_COMM_FAILURE(4) BAD_TIMEOUT(5) BAD_ADDRESS(6)
BAD_TYPE_MISMATCH(7) BAD_CONFIG_ERROR(8) BAD_DEVICE_FAILURE(9)`.

**Write status**: `PENDING, SUCCESS, MISMATCH, FAILED, UNCERTAIN, REJECTED`.

---

## 5. Normalized data model (`TagReading.to_dict()`)

```json
{"reading_uid": "019a7b2e-…", "plant_id": "plant_01", "device_id": "siemens_01",
 "device_name": "Siemens S7-1200", "tag_id": "temperature", "tag_name": "Temperature",
 "value": 25.5, "raw_value": 25.5, "data_type": "REAL", "value_type": "float", "quality": "GOOD",
 "source_timestamp": "2026-11-12T07:15:01.123456+00:00", "timestamp_origin": "gateway_read",
 "gateway_timestamp": "2026-11-12T07:15:01.125001+00:00", "engineering_unit": "C",
 "logging_mode": "cyclic", "error_code": null}
```

`TagEvent`: `event_uid, plant_id, device_id, tag_id (null for device events), event_type,
event_timestamp, previous_value, new_value, previous_quality, new_quality, threshold, message`.
Types: `VALUE_CHANGE, RISING_EDGE, FALLING_EDGE, THRESHOLD_HIGH, THRESHOLD_LOW, THRESHOLD_CLEAR,
QUALITY_CHANGE, COMM_LOST, COMM_RESTORED`.

Canonical data types: `BOOL SINT INT DINT LINT USINT UINT UDINT ULINT BYTE WORD DWORD LWORD REAL
LREAL STRING WSTRING CHAR` (+ `array_length`). Each driver validates its supported subset.

---

## 6. Configuration

Files: `config/settings.yaml`, `config/plants.yaml`, tag files referenced by `tags_file`,
`config/logging.yaml`. Secrets only in environment / `.env`, referenced by variable **name**.
`config/sim/` is a complete configuration for the local simulators.

Address grammar per driver:

| Driver | Examples | Notes |
|---|---|---|
| `siemens_s7` | `DB100.DBX10.0` `DB100.DBB2` `DB100.DBW8` `DB100.DBD0` `MW10` `M5.1` `I0.0` `QW64` | Size letter must match the type; `B` for 8-byte types, strings, arrays. Non-optimized DBs only. |
| `rockwell_logix` | `Line1_Temp` `Program:Main.Counter` `Recipe[0]` + `array_length` | Checked against the controller tag list on connect. |
| `mitsubishi_mc` | `D100` `M10` `X17` `W1A` `D100.3` (read-only bit) | FX X/Y octal; Q/L/iQ-R X/Y/B/W hex. |
| `modbus_tcp/rtu` | `holding:0` `input:100` `coil:5` `discrete:3` `holding:10.3` `2@holding:0` | Zero-based; `4xxxx` rejected; `unit@` prefix for multi-slave buses. |
| `opc_ua` | `ns=3;s="DB_Process"."Temperature"` | Standard NodeId. |
| `opc_da` | `Channel1.Device1.Tag1` | Item ID. |

Tag options: `poll_interval_ms` (≥100), `logging_mode` (`cyclic|event|both|none`), `scaling`,
`deadband` (`absolute|percent`), `thresholds` (high/low/hysteresis), `edge_events`, `access`
(`read_only|read_write`), `write` (`min, max, allowed_roles, verify_readback, readback_tolerance, timeout_s`).
Config is validated with all errors listed (`python scripts/validate_config.py`).

---

## 7. Database (MySQL 8.x)

`database/migrations/001_initial.sql` (copy: `database/schema.sql`), applied by
`scripts/init_database.py`. Tables: `plants, plc_devices, plc_tags, plc_tag_history,
plc_tag_latest, plc_events, plc_write_commands, gateway_health, audit_logs` plus
`schema_migrations, quality_codes, value_type_codes` and Grafana views
`v_tags, v_tag_latest, v_tag_history, v_events`. All timestamps `DATETIME(6)` UTC.

`plc_tag_history` PK `(tag_pk, gateway_ts, reading_uid)`, secondary `(gateway_ts)`, daily
partitions. Verified plan for a Grafana per-tag range query: views merged, PK range scan,
partition pruning.

### 7.1 Volume (calculated)

| Quantity | Value |
|---|---|
| Readings/min · /s · /h · /day | 4,000 · 66.7 · 240,000 · 5,760,000 |
| Estimated size per row (data + PK + secondary index) | ~100–130 bytes → ~0.6–0.75 GB/day, ~55–68 GB at 90 days |
| Insert statements | 1 multi-row INSERT per flush (default 1 s) + 1 latest upsert |

Row size is an estimate; measure on the target with
`SELECT PARTITION_NAME, DATA_LENGTH+INDEX_LENGTH FROM information_schema.PARTITIONS WHERE TABLE_NAME='plc_tag_history'`.

### 7.2 Retention

`RecoveryService` (after DB becomes available, then daily at `maintenance_hour_utc`, at most every
6 h): creates partitions `partitions_ahead_days` ahead, drops expired history/event partitions
(optional CSV.gz export first), deletes old `gateway_health` rows; `audit_logs` retention only
with a separate maintenance user (`MYSQL_MAINT_USER`).

---

## 8. Buffering, recovery, backpressure

- Producer side never blocks: bounded queue (`queue_max_size`) + bounded overflow list; beyond
  that, records are dropped **and counted** (`records_dropped_total{reason}`, error log, health).
- `spool_first`: flusher commits queue → SQLite every `flush_interval_ms`; `BatchWriter` reads oldest
  rows, commits to MySQL in one transaction (history + latest + events), then deletes them.
- MySQL down: batches stay in the spool; `DatabaseUnavailableError` closes the pool and clears the
  registry so the writer rebuilds the pool and re-syncs automatically (tested).
- Spool full (`spool_max_rows`): `drop_cyclic_keep_events` (default) | `drop_oldest` | `reject_new` — counted.
- **Crash / power failure**: on start, existing spool rows are replayed oldest first. Rows that
  were committed to MySQL but not yet deleted from the spool are deduplicated by UUID. Loss
  window: in-memory queue only (≤ `flush_interval_ms`). Graceful stop flushes the queue.
- PLC writes are never spooled or replayed.
- Device reconnect: exponential backoff with jitter + circuit breaker (CLOSED → OPEN after
  `failure_threshold` → HALF_OPEN probe). One `COMM_LOST` event per outage, one `BAD_NOT_CONNECTED`
  reading per tag (quality change only), one `COMM_RESTORED` on recovery.

---

## 9. Write path and safety (`core/write_manager.py`)

```
WriteManager.submit(principal, device_id, tag_id, value, request_id, reason)
 1. request_id format; device/tag exist                                  → NOT_FOUND
 2. principal role ∈ tag.write.allowed_roles                             → FORBIDDEN
 3. request_id known → return stored outcome (never re-executed); in progress → IN_PROGRESS
 4. rate limit per principal (writes.rate_limit_per_min)                 → RATE_LIMITED
 5. gateway.read_only_mode=false, env kill switch != 1, kill file absent → GATEWAY_READ_ONLY / KILL_SWITCH_*
 6. device.write_enabled, tag.access=read_write, tag enabled             → DEVICE_READ_ONLY / TAG_READ_ONLY
 7. strict type validation, engineering min/max                          → INVALID_TYPE / ABOVE_MAX / BELOW_MIN
 8. persist PENDING command (audit store unavailable → AUDIT_UNAVAILABLE, fail closed)
 9. device connected? else FAILED (DEVICE_NOT_CONNECTED)
10. write with timeout: timeout/connection loss after send → UNCERTAIN (never auto-retried)
11. read-back verification → SUCCESS | MISMATCH | UNCERTAIN; persist + audit
```

Only configured tags are writable — there is no raw-address write path. The gateway is not a
safety system and cannot bypass PLC interlocks (see SECURITY.md §6).

---

## 10. Integration boundary

`IntegrationAdapter` (`start, stop, publish_readings, publish_events, health`) in
`integrations/base.py`; `IntegrationDispatcher` with one bounded queue per adapter.
Implemented: `rest_forwarder` (HTTPS POST, bearer token from env, durable SQLite outbox,
at-least-once — receiver deduplicates by UUID) and `mqtt_publisher` (paho-mqtt 2, TLS, QoS).
SAP: interface only; no SAP API is assumed.

---

## 11. Observability

- Logs: console + `logs/gateway.log` (JSON, 20 MB × 10) + `logs/gateway_errors.log`; fields
  `ts, level, logger, plant_id, device_id, driver, tag_id, operation, error_code, duration_ms,
  request_id, principal, message`; secrets redacted.
- `data/status.json` (every ≤10 s): overall status, devices, DB, buffer, writes, process, metrics.
- `gateway_health` table (every `health.snapshot_interval_s`): per device and per gateway → Grafana.
- Metrics: `plc_connected, reads_ok_total, reads_failed_total, poll_duration_ms, scan_overruns_total,
  reconnects_total, connect_failures_total, events_total, records_written_total,
  records_dropped_total{reason}, db_insert_duration_ms, db_write_failures_total, queue_size,
  spool_rows, spool_append_ms`.

---

## 12. Project structure

```
industrial-plc-gateway/
├── main.py                      entry point (run / --validate-only)
├── pyproject.toml  requirements.txt  requirements-dev.txt  requirements-windows.txt
├── .env.example  .gitignore  docker-compose.dev.yml (dev only)  IMPLEMENTATION_CHECKLIST.md  README.md
├── config/                      settings, plants, tags*, logging   + config/sim/ (simulator setup)
├── drivers/                     base, siemens_s7, rockwell, mitsubishi, mc_protocol, modbus_common,
│   │                            modbus_tcp, modbus_rtu, opc_ua, opc_da, opc_da_bridge, serial
│   └── codecs/                  datatypes, siemens_address, mitsubishi_address, register_codec
├── core/                        config_loader, connection_manager, driver_manager, tag_manager,
│                                polling_scheduler, device_worker, data_normalizer, event_detector, write_manager
├── services/                    gateway_service, buffer_service, health_service, recovery_service, metrics
├── database/                    mysql_client, repository, batch_writer, schema.sql, migrations/, grants_example.sql
├── integrations/                base, rest_forwarder, mqtt_publisher
├── models/                      config, plc, tag, reading, events
├── utils/                       logger, validators, exceptions, backoff, timeutil
├── simulators/                  s7_sim, modbus_sim, mc_sim, opcua_sim, __main__ (python -m simulators)
├── tests/                       unit, simulation (sim), MySQL (mysql), hardware/ (hardware)
├── perf/                        load_test.py (workloads A/B/C), report.py, results/
├── scripts/                     validate_config, init_database, discover_tags, test_write
├── grafana/                     provisioning/, dashboards/*.json, build_dashboards.py
├── deploy/                      install_mac.sh, install_windows.ps1, run_gateway.sh/.bat, gateway_service.py
└── docs/                        ARCHITECTURE, COMPATIBILITY, PLC_CONFIGURATION, INSTALLATION,
                                 SECURITY, GRAFANA, PERFORMANCE, TROUBLESHOOTING
```

Changes from the original requested tree: `api/` removed (owner decision); `test_scheduler.py`
merged into `tests/test_core_logic.py`; files marked in the tree above were added.

---

## 13. Migration from Kepware / Node-RED / FTTM

| Existing | Replacement | Precondition |
|---|---|---|
| Kepware channels/devices | `plants.yaml` | Export Kepware project |
| Kepware tags | tag YAML via `scripts/discover_tags.py kepware-csv` (each tag flagged for review) | Kepware CSV export |
| Kepware DB logging | `BatchWriter` → MySQL; SQL views for existing consumers | Map consumers |
| Node-RED PLC flows | DeviceWorkers + EventDetector + adapters | Flow inventory |
| KepServer VB scripts / FTTM | Case-by-case | **Inventory / data contract required** |

Recommended: run read-only in parallel with Kepware ≥ 1 week, compare values, cut over consumers one by one.
