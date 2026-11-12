# Troubleshooting

Start with `data/status.json` (device status, last error, DB state, spool, dropped records) and
`logs/gateway_errors.log`. Every log line carries `device_id`, `operation` and `error_code`.

## Configuration

| Symptom | Cause / action |
|---|---|
| `CONFIGURATION ERROR` on start | All problems are listed; fix them and run `python scripts/validate_config.py`. |
| `size letter W does not match REAL` | S7 address letter must match the type: `DBD` for REAL/DINT, `DBW` for INT/WORD, `DBX x.y` for BOOL. |
| `1-based register notation is not accepted` | Use `holding:0` for 40001 (see PLC_CONFIGURATION.md §4). |
| `not available on platform darwin` | OPC DA device enabled on macOS — disable it or run on Windows. |
| `both open serial port` | One device per RS485 bus; use `unit@holding:…` for multiple slaves. |

## Siemens S7

| Symptom | Cause / action |
|---|---|
| connect fails, `Connection refused` | Wrong IP, firewall, or TCP 102 blocked. `Test-NetConnection <ip> -Port 102`. |
| connect fails after TCP connect (ISO / COTP error) | Wrong rack/slot (S7-1200/1500: 0/1, S7-300: 0/2). |
| tags `BAD_ADDRESS` `S7_ADDRESS_REJECTED` | PUT/GET disabled, DB missing, DB **optimized**, or offset beyond DB length. |
| all reads fail with access errors on new firmware | CPU protection level / secure-communication-only setting blocks PUT/GET. |
| `BAD_TYPE_MISMATCH S7_DECODE` on STRING | Wrong `string_length` or offset; check the DB offset column. |

## Rockwell

| Symptom | Cause / action |
|---|---|
| `LOGIX_TAG_NOT_FOUND` | Name not in controller tag list (check program scope prefix, spelling, external access). |
| `LOGIX_TYPE_MISMATCH` | Configured `data_type` differs from the controller type. |
| connect fails | Wrong `path` slot, CIP Security enforced, or controller connection limit reached. |

## Mitsubishi

| Symptom | Cause / action |
|---|---|
| `MC_END_0xC056` | Device/address out of range for the CPU. |
| `MC_END_0xC059` | Command not supported: frame type (3E vs 1E) or binary/ASCII mismatch in PLC settings. |
| `MC_END_0x0055` / writes fail | "Write during RUN" not enabled in the PLC Ethernet settings. |
| inputs wrong on FX | X/Y are octal on FX: `X10` is the 9th input. |
| 1E abnormal code `0x10` | PC number wrong (use 0xFF) or adapter not in MC protocol mode. |

## Modbus

| Symptom | Cause / action |
|---|---|
| `MODBUS_ILLEGAL_DATA_ADDRESS` | Offset not mapped in device (zero-based!). Others in the same block are retried individually. |
| plausible-but-wrong floats | Byte/word order: try `word_order: little` (CDAB). |
| RTU timeouts | Baud/parity/stop bits, unit id, RS485 A/B polarity, missing termination, `timeout_s` too short. |

## OPC UA

| Symptom | Cause / action |
|---|---|
| `BadSecurityChecksFailed` / connect rejected | Server does not trust the client certificate yet — trust it on the server. |
| `BadCertificateUriInvalid` | `application_uri` does not match the certificate's SAN URI. |
| `BAD_ADDRESS OPCUA_BadNodeIdUnknown` | NodeId wrong (namespace index can change — browse with `discover_tags.py opcua`). |

## Database / buffering

| Symptom | Cause / action |
|---|---|
| status `database.available=false`, `spool_rows` rising | MySQL unreachable or credentials wrong; data is kept in the spool and replayed automatically. |
| `records_dropped_total` > 0 | Queue or spool overflow (see `reason`): MySQL outage longer than the spool capacity, or sustained overload. Increase `spool_max_rows` / disk, fix MySQL. |
| `has no p_future partition` | Schema not created with `scripts/init_database.py`. |
| writes rejected `AUDIT_UNAVAILABLE` | Writes require MySQL (audit) — by design (fail closed). |
| `escape_bytes_prefixed` / `'str' object is not callable` | PyMySQL 1.2.x installed — reinstall the pinned `PyMySQL==1.1.2`. |

## Writes

| Code | Meaning |
|---|---|
| `GATEWAY_READ_ONLY` | `gateway.read_only_mode: true` (default). |
| `KILL_SWITCH_FILE` / `KILL_SWITCH_ENV` | `data/WRITE_DISABLE` exists or `GATEWAY_WRITE_DISABLE=1`. |
| `DEVICE_READ_ONLY` / `TAG_READ_ONLY` | `write_enabled` / `access: read_write` not set. |
| `FORBIDDEN` | `--role` not in the tag's `write.allowed_roles`. |
| `UNCERTAIN` | Timeout after sending — check the PLC value before re-issuing with a new request id. |
| `MISMATCH` | PLC accepted the write but the read-back differs (PLC logic overwrote it?). |
