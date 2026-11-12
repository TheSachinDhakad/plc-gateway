# Security

## 1. Threat model

| Asset | Threat | Controls |
|---|---|---|
| PLC process state | Unauthorized or erroneous writes | No network API; read-only default; write gates; allowlist; limits; roles; kill switch; audit |
| PLC availability | Polling overload, connection exhaustion | One connection per device, batched reads, min poll 100 ms, circuit breaker |
| Historical data | Loss, tampering | Durable spool, idempotent inserts, append-only audit, least-privilege DB users |
| Credentials | Leakage via config or logs | Env-only secrets, redaction filter, no secrets in DB or status file |
| Gateway host | Compromise from IT network | No listening ports, firewall, dedicated service account, file ACLs |

The gateway **opens no listening network port**. It only makes outbound connections to PLCs,
MySQL and configured integration endpoints.

## 2. Network segmentation (IEC 62443 zones/conduits)

```
 [ Enterprise IT ] ─ fw ─ [ DMZ: MySQL, Grafana, central server ] ─ fw ─ [ OT cell: gateway IPC ─ PLCs ]
```

- PLC ports (102 S7, 44818 EtherNet/IP, 502 Modbus, MC/SLMP port, 4840 OPC UA) reachable **only**
  from the gateway IPC.
- Gateway outbound: PLC subnet; MySQL 3306 (TLS recommended); integration endpoints (443 / 8883).
- Grafana reaches MySQL only; users reach Grafana, never PLCs or the gateway.
- **Never** expose PLC ports, MySQL or Grafana to the internet; remote access only through the
  site's managed VPN / jump host.

## 3. Secrets

- Only in environment variables / `.env` (git-ignored; `.env.example` holds placeholders).
- YAML references variable **names** (`password_env: OPCUA_PASSWORD`, `token_env: …`).
- Log redaction filter for passwords, tokens, API keys, authorization headers, DSN passwords.
- `plc_devices.endpoint` stores host/endpoint only; `status.json` contains no credentials.
- Windows: `install_windows.ps1` removes inheritance on `.env`, `certs`, `data`, `logs` and grants
  only Administrators, SYSTEM and the service account.

## 4. Database

- `database/grants_example.sql`: `gw_app` (INSERT/UPDATE on data tables, **INSERT-only on
  `audit_logs`**, ALTER/DROP only on the two partitioned tables), optional `gw_maint` (audit
  retention), `gw_grafana` (SELECT on views/health only). No SUPER/FILE/GRANT OPTION.
- Schema creation (`scripts/init_database.py`) runs once with an administrative account.
- All SQL parameterized; identifiers never from input (partition names generated and regex-checked;
  retention tables allowlisted).
- TLS: set `MYSQL_SSL_CA` to verify the server certificate.

## 5. Writes

1. Read-only by default at three levels: `gateway.read_only_mode: true`, `device.write_enabled: false`,
   `tag.access: read_only`.
2. Only explicitly configured tags can be written; no raw-memory writes exist in code.
3. Role check against `tag.write.allowed_roles`; operator identity recorded.
4. Strict type validation (no implicit string→number), engineering min/max.
5. Kill switch without restart: file `data/WRITE_DISABLE` or env `GATEWAY_WRITE_DISABLE=1`.
6. Audit store must be available, otherwise writes are rejected (fail closed).
7. Every attempt — rejected or executed — is written to `audit_logs`; executed commands to
   `plc_write_commands` with requested and read-back values.
8. Uncertain outcomes (timeout after send) are never retried automatically; re-issue requires a new
   `request_id` after checking the PLC.

## 6. The gateway is not a safety system

Safety functions stay in the PLC / safety PLC. Commands written by the gateway (setpoints,
start/stop requests) must be validated by PLC logic (ranges, permissives, interlocks, handshake
bits). The gateway cannot and does not bypass them.

## 7. Protocol security realities

| Protocol | Native security | Mitigation |
|---|---|---|
| S7 (PUT/GET) | None | Segmentation; enable PUT/GET only where needed; keep write DBs separate; prefer S7-1500 OPC UA where possible |
| EtherNet/IP CIP | CIP Security exists, **not supported by pycomm3** | Segmentation; controller key switch / trusted slot |
| MC / SLMP | None (optional remote password on some modules) | Segmentation; disable "write during RUN" unless needed |
| Modbus TCP/RTU | None | Segmentation; read-only registers where possible |
| OPC UA | Sign/SignAndEncrypt, certificates, user auth | `Basic256Sha256`+ with `SignAndEncrypt`; pinned server certificate; `None` only with `allow_insecure: true` (logged) |
| OPC DA | DCOM security | Local server only; no DCOM across zones |

## 8. Host hardening (Windows IPC)

- Run the `PLCGateway` service under a dedicated local account (`-ServiceAccount`), not LocalSystem,
  with "Log on as a service" right only.
- Installation directory read-only for the service account; writable: `data\`, `logs\`.
- Windows Firewall: no inbound rules needed; restrict outbound to PLC subnet, MySQL and integrations.
- OS patching per plant change management; disable unused services; BitLocker on the IPC disk.
