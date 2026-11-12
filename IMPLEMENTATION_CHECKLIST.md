# Implementation Checklist

Ticked = implemented **and** exercised by tests or a run in this repository. Hardware
verification is tracked separately (bottom) and in docs/COMPATIBILITY.md.

## Architecture & compatibility
- [x] Library APIs verified by installing and introspecting (Python 3.11, 2026-11-12)
- [x] docs/ARCHITECTURE.md, COMPATIBILITY.md (incl. implementation findings), SECURITY.md

## Project & configuration
- [x] requirements*.txt / pyproject.toml with exact pins (PyMySQL pinned to 1.1.2 — see COMPATIBILITY §5)
- [x] .env.example (placeholders only), .gitignore
- [x] config/ (production example, read-only default) and config/sim/ (simulators) — both validate
- [x] Pydantic config models + loader with cross-file and per-driver address validation (tests/test_config.py)
- [x] scripts/validate_config.py

## Drivers
- [x] Common driver contract (drivers/base.py)
- [x] Siemens S7 — read/write vs snap7.Server (bit-write semantics need hardware verification)
- [x] Rockwell Logix — tested with a fake pycomm3 client only (no CIP simulator)
- [x] Mitsubishi MC 3E (FX5U/Q/iQ-R) and 1E (FX3U) — own framing, tested vs independent simulator
- [x] Modbus TCP — tested vs independent simulator (all byte/word orders, exceptions, writes)
- [x] Modbus RTU — offline tests only (no serial hardware)
- [x] OPC UA — tested vs asyncua server (types, timestamps, bad nodes, writes, server loss)
- [x] OPC DA — implemented (COM bridge, Windows-only); quality mapping tested; **no runtime test**
- [x] Serial helpers

## Core engine
- [x] Connection manager (backoff+jitter, circuit breaker, single COMM_LOST/RESTORED per outage)
- [x] Driver manager (per-device executors), tag manager (+ latest cache), polling scheduler (drift-free, overruns)
- [x] Data normalizer (scaling, typed values), event detector (change/deadband/edges/thresholds/quality)
- [x] Write manager (gates, idempotency, kill switches, audit, read-back, uncertain handling)

## Persistence & services
- [x] MySQL schema + migrations + partitions + views (tested on MySQL 8.4.6)
- [x] Repository, batch writer (idempotent, rollback, pool rebuild after outage)
- [x] SQLite spool (spool_first / memory_first), crash replay, overflow policies — tested
- [x] Gateway service, health service (status.json + gateway_health), recovery/retention service
- [x] Integrations: REST forwarder (durable outbox), MQTT publisher — **not exercised against real endpoints**
- [x] ~~REST API~~ — removed at project owner's request

## Tests, simulators, performance
- [x] Simulators: S7, Modbus TCP, MC 3E/1E, OPC UA (`python -m simulators`)
- [x] Unit + simulation + MySQL + end-to-end tests (see README for commands)
- [x] Hardware smoke tests (tests/hardware, opt-in)
- [x] perf/load_test.py (workloads A/B/C) + perf/report.py
- [ ] 15-minute load runs A/B/C executed and reported in docs/PERFORMANCE.md

## Deployment & documentation
- [x] install_mac.sh (syntax-checked), run_gateway.sh
- [x] install_windows.ps1, run_gateway.bat, pywin32 service host — **written, not executed on Windows**
- [x] Grafana provisioning + 5 dashboards (SQL checked against MySQL)
- [x] README, INSTALLATION, PLC_CONFIGURATION, TROUBLESHOOTING, GRAFANA, PERFORMANCE
- [ ] ZIP archive

## Hardware validation (site commissioning)
- [ ] S7-1200 DB100 read (PUT/GET, non-optimized) — `pytest -m hardware`
- [ ] S7-1200 test write DB100.DBD12 and a DBX bit write
- [ ] ControlLogix L7x / L8x
- [ ] FX5U (SLMP 3E) / FX3U (ENET-ADP 1E)
- [ ] Modbus TCP / RTU field devices
- [ ] OPC UA server with certificates
- [ ] OPC DA server (Windows)
- [ ] Windows service install / stop / restart-on-failure on the target IPC
- [ ] 24-hour read-only soak in parallel with Kepware
