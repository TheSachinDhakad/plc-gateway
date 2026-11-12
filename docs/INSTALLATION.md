# Installation

## 1. Requirements

| Item | Requirement |
|---|---|
| Python | 3.11 (recommended) or 3.12, 64-bit |
| MySQL | 8.0.19+ or 8.4 LTS, InnoDB, UTC (`time_zone` is set per session by the gateway) |
| OS | Windows 10/11 (production), macOS (development), Linux (works, not the target) |
| Disk | Spool sized for the longest expected MySQL outage: measured ≈ 530 B per record → 24 h at 4,000/min ≈ 3 GB; default `spool_max_rows` 20 M ≈ 10.6 GB (≈ 3.5 days) |
| Network | Outbound to PLCs, MySQL, integrations; no inbound ports |

## 2. macOS (development)

```zsh
cd industrial-plc-gateway
./deploy/install_mac.sh            # creates .venv (3.11/3.12), installs deps, creates .env
source .venv/bin/activate
```

Local MySQL options: an existing server, or `docker compose -f docker-compose.dev.yml up -d`
(MySQL 8.4 on 127.0.0.1:3307 + Grafana on :3000; set the `DEV_*` variables first).

```zsh
python scripts/init_database.py --config-dir config/sim     # schema + partitions (admin user)
python -m simulators                                       # S7 :1102, Modbus :1502, MC :5000, OPC UA :4841
python main.py --config-dir config/sim                     # gateway against the simulators
cat data/sim/status.json                                   # live status
pytest                                                     # unit + simulation tests
MYSQL_TEST_DSN=mysql://user:pw@127.0.0.1:3306/plc_gateway_test pytest -m mysql   # disposable DB!
```

## 3. Windows 10/11 industrial PC (production)

1. Install Python 3.11 64-bit from python.org ("py" launcher, all users).
2. Copy the project to `C:\Program Files\industrial-plc-gateway` (or another admin-only path).
3. Elevated PowerShell in that directory:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\deploy\install_windows.ps1                                   # venv, deps, ACLs, validation
   ```
4. Edit `.env` (MySQL credentials) and `config\plants.yaml`, `config\tags*.yaml`.
5. Create the schema with an administrative MySQL account, then create least-privilege users:
   ```powershell
   .venv\Scripts\python.exe scripts\init_database.py --dsn "mysql://admin:***@db-host:3306/plc_gateway"
   # then run database\grants_example.sql (edited) as the MySQL administrator
   ```
6. Validate and test in the foreground:
   ```powershell
   .venv\Scripts\python.exe scripts\validate_config.py
   deploy\run_gateway.bat                       # Ctrl+C to stop
   ```
7. Install and start the service:
   ```powershell
   .\deploy\install_windows.ps1 -InstallService -ServiceAccount ".\svc_plcgw"
   Start-Service PLCGateway
   Get-Service PLCGateway
   Stop-Service PLCGateway
   ```
   Recovery actions: restart after 5 s / 30 s / 60 s. The service stops gracefully (queue flushed to the spool).

> The PowerShell installer and the pywin32 service host were written for Windows but have not
> been executed in this repository's CI (development was on macOS). Validate them on the target
> image before rollout. Alternative service wrapper: WinSW running `deploy\run_gateway.bat`.

## 4. Locations

| What | Where |
|---|---|
| Logs | `logs\gateway.log` (JSON), `logs\gateway_errors.log`, console |
| Live status | `data\status.json` |
| Spool | `data\spool.sqlite3` (+ `-wal`, `-shm`) — never delete while the service runs |
| Write kill switch | create `data\WRITE_DISABLE` or set `GATEWAY_WRITE_DISABLE=1` |
| Service events | Windows Event Log → Application → source `PLCGateway` |

## 5. Upgrades

1. Stop the service (queue is flushed to the spool; it replays on start).
2. Replace code, keep `.env`, `config\`, `data\`, `logs\`.
3. `pip install -r requirements-windows.txt`, `python scripts/init_database.py` (applies new migrations only).
4. `python scripts/validate_config.py`, start the service, check `status.json`.
