# industrial-plc-gateway

A Python data gateway that reads PLCs directly over their native protocols, normalizes the
values, stores them in MySQL (for Grafana and other consumers), and supports tightly controlled,
audited writes. It is intended to replace Kepware / Node-RED / KepServer-script data acquisition
where the PLC protocol is supported.

| Protocol | Driver | Typical PLCs | Status |
|---|---|---|---|
| Siemens S7 (PUT/GET) | `siemens_s7` (python-snap7) | S7-300/400/1200/1500 | simulation-tested |
| EtherNet/IP CIP | `rockwell_logix` (pycomm3) | ControlLogix L7x/L8x, CompactLogix | unit-tested only |
| MC protocol 3E / 1E | `mitsubishi_mc` (own implementation) | FX5U, Q, L, iQ-R / FX3U-ENET-ADP | simulation-tested |
| Modbus TCP | `modbus_tcp` (pymodbus) | meters, drives, any Modbus server | simulation-tested |
| Modbus RTU | `modbus_rtu` (pymodbus + pyserial) | RS485/RS232 slaves | unit-tested only |
| OPC UA | `opc_ua` (asyncua) | S7-1500 server, any OPC UA server | simulation-tested |
| OPC DA | `opc_da` (pywin32 COM bridge) | legacy OPC DA servers, Windows only | partial, untested |

**No PLC model is hardware-verified yet.** Run the hardware smoke tests during commissioning
and record results in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

## Quick start (macOS, against the built-in simulators)

```zsh
./deploy/install_mac.sh && source .venv/bin/activate
# MySQL: set MYSQL_* in .env (or docker compose -f docker-compose.dev.yml up -d)
python scripts/init_database.py --config-dir config/sim
python -m simulators                       # terminal 1
python main.py --config-dir config/sim     # terminal 2
cat data/sim/status.json
```

## Production (Windows 10/11)

```powershell
.\deploy\install_windows.ps1                       # venv, dependencies, ACLs, config validation
# edit .env and config\*.yaml, then:
.venv\Scripts\python.exe scripts\init_database.py --dsn "mysql://admin:***@db:3306/plc_gateway"
.\deploy\install_windows.ps1 -InstallService -ServiceAccount ".\svc_plcgw"
Start-Service PLCGateway
```

See [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Everyday commands

| Task | Command |
|---|---|
| Validate configuration (no PLC I/O) | `python scripts/validate_config.py [--config-dir …] [--show-tags]` |
| Run in foreground | `python main.py` / `deploy\run_gateway.bat` |
| Create / migrate schema | `python scripts/init_database.py` |
| Discover / import tags | `python scripts/discover_tags.py opcua|logix|kepware-csv …` |
| Commissioning test write (audited, all gates) | `python scripts/test_write.py --device-id … --tag-id … --value … --operator … --reason …` |
| Emergency: block all writes | create `data/WRITE_DISABLE` or set `GATEWAY_WRITE_DISABLE=1` |
| Tests | `pytest` (unit + simulation), `pytest -m mysql` (needs `MYSQL_TEST_DSN`), `pytest -m hardware` |
| Load test | `python perf/load_test.py --workload A|B|C --duration 900` |
| Grafana dashboards | `grafana/dashboards/*.json` (see [docs/GRAFANA.md](docs/GRAFANA.md)) |

## Configuration in one minute

`config/plants.yaml` lists plants → devices (driver + connection). Each device points at a tag
file. A tag:

```yaml
- tag_id: temperature
  device_id: siemens_01
  name: Temperature
  address: DB100.DBD0          # driver-specific address
  data_type: REAL
  poll_interval_ms: 1000
  logging_mode: cyclic         # cyclic | event | both | none
  engineering_unit: C
  deadband: {type: absolute, value: 0.5}
  thresholds: {high: 80, low: 5, hysteresis: 1}
```

Secrets live only in `.env`. The gateway is **read-only by default**: writes need
`gateway.read_only_mode: false`, `device.write_enabled: true`, `tag.access: read_write`, a role
in `tag.write.allowed_roles`, and pass type/limit checks; every attempt is audited.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [Compatibility & findings](docs/COMPATIBILITY.md) ·
  [PLC configuration](docs/PLC_CONFIGURATION.md) · [Installation](docs/INSTALLATION.md) ·
  [Security](docs/SECURITY.md) · [Grafana](docs/GRAFANA.md) · [Performance](docs/PERFORMANCE.md) ·
  [Troubleshooting](docs/TROUBLESHOOTING.md) · [Checklist](IMPLEMENTATION_CHECKLIST.md)
