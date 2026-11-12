# Compatibility Matrix

Status legend:

- **Planned-Full** — driver will implement the documented operations completely; hardware validation still required.
- **Planned-Partial** — some operations/models deliberately out of scope; the driver fails explicitly for them.
- **Not supported** — rejected at config validation with a clear error.
- **HW-Verified** — confirmed on real hardware (none yet; this column is filled in during commissioning).

Nothing in this document is HW-Verified yet. Simulation tests do not count as hardware verification.

**Simulation-verified** (this repository, 2026-11-12, `pytest -m sim`): Siemens S7 read/write against
`snap7.Server`; Modbus TCP against an independent MBAP server; MC 3E (FX5U, Q, iQ-R framing) and
MC 1E (FX3U framing) against an independent MC server; OPC UA against an asyncua server.
Not simulation-tested: Rockwell Logix (no CIP simulator), Modbus RTU (needs serial hardware or a
virtual port pair), OPC DA (Windows COM).

---

## 1. Verified library facts (2026-11-12)

Collected by installing each package into a clean Python 3.11 venv on macOS (arm64) and
introspecting its API, plus PyPI release metadata.

| Package | Latest | Released | Requires-Python | Verified facts relevant to design |
|---|---|---|---|---|
| python-snap7 | 3.5.0 | 2026-11-11 | ≥3.10 | **Pure-Python** implementation (no native snap7 DLL shipped). `Client.connect(address, rack, slot, tcp_port=102)`, `db_read`, `db_write`, `read_area(Area, db, start, size)`, `read_multi_vars`, `get_cpu_info`, `get_cpu_state`, `set_connection_type`. `snap7.util.get_*/set_*` for BOOL…LREAL, STRING, WSTRING, DTL. `snap7.Server` available for simulation. `AsyncClient` present (new). **No S7CommPlus.** Released one day ago → pin and validate. |
| pycomm3 | 1.2.16 | 2025-12-22 | ≥3.6.1 | `LogixDriver(path, init_tags=True, init_program_tags=True)`, `read(*tags)`, `write(*(tag, value))`, `get_plc_info`, `get_tag_list`. Synchronous (needs executor). No CIP Security (TLS). |
| pymodbus | 3.16.2 | 2026-11-03 | ≥3.10 | Async clients; **unit id parameter is `device_id=`** (older `slave=`/`unit=` removed). `pymodbus.payload` **removed**; `convert_from_registers/convert_to_registers` handle **word order only**. |
| asyncua | 2.1.0 | 2026-10-06 | ≥3.10 | `Client(url, timeout, watchdog_intervall, auto_reconnect, …)`, `set_security_string`, `set_security`, `set_user/set_password`, `load_client_certificate`, `read_values`, `write_values`, `create_subscription`, `check_connection`. |
| pymcprotocol | 0.3.0 | 2021-04-26 | — | 3E/4E frames only; `plctype ∈ {Q, L, QnA, iQ-L, iQ-R}`; no 1E frame; no FX handling. **Not used** (stale) → own implementation. |
| pyserial | 3.5 | 2020-11-23 | — | Stable; de-facto standard. Used for port discovery and by pymodbus serial. |
| OpenOPC-Python3x | 1.3.1 | 2020-03-03 | — | Unmaintained. **Not used.** OPC DA implemented directly via pywin32 COM. |
| pywin32 | 312 | 2026-06-04 | ≥3.9 | Windows service + COM. Windows only. |
| aiomysql | 0.3.2 | 2025-10-22 | ≥3.9 | Async pool over PyMySQL (pure Python). Chosen DB client; requires PyMySQL 1.1.x (see below). |
| PyMySQL | 1.1.2 (pinned) | — | — | **1.2.x is incompatible with aiomysql 0.3.2**: `escape_bytes_prefixed` became a sentinel string, so every `bytes` parameter (BINARY(16) UUIDs) fails. Found by `tests/test_database.py`. |

**Python runtime:** 3.11 (primary) / 3.12. The workstation's current default venv is
Python 3.14.7, which is outside the validated range — the installers create a separate
3.11 venv (`/opt/homebrew/bin/python3.11` is present on this Mac).

---

## 2. PLC / protocol matrix

| Family / model | Protocol | Driver | Library | Read | Write | Batch read | Status | Key prerequisites & limits |
|---|---|---|---|---|---|---|---|---|
| Siemens S7-300 | S7 (ISO-on-TCP, 102) | `siemens_s7` | python-snap7 | ✔ | ✔ | ✔ multi-var | Planned-Full | Rack 0, slot **2** typical. CPU with integrated PN or CP343-1. No LREAL/LINT types on S7-300. |
| Siemens S7-400 | S7 | `siemens_s7` | python-snap7 | ✔ | ✔ | ✔ | Planned-Full (not in spec, comes free) | Slot per HW config (often 3). |
| Siemens S7-1200 | S7 (PUT/GET) | `siemens_s7` | python-snap7 | ✔ | ✔ | ✔ | Planned-Full | Rack 0, slot **1** (verify). **PUT/GET must be enabled** in TIA Portal; **DBs must be non-optimized** for absolute addressing; access level must permit it. FW ≥ V4.x with protection levels — see §3. |
| Siemens S7-1500 | S7 (PUT/GET) | `siemens_s7` | python-snap7 | ✔ | ✔ | ✔ | Planned-Full | Same as S7-1200; slot **1**. PDU 480–960 bytes. |
| Siemens S7-1200/1500 optimized DBs | S7CommPlus | — | — | ✘ | ✘ | ✘ | **Not supported** | Use built-in OPC UA server via `opc_ua` driver instead. |
| Siemens S7-1500 / S7-1200 (FW ≥ V4.4) OPC UA server | OPC UA | `opc_ua` | asyncua | ✔ | ✔ | ✔ | Planned-Full | Server must be activated in TIA Portal; runtime licence required; symbolic access to optimized DBs. |
| Rockwell ControlLogix L7x | EtherNet/IP CIP | `rockwell_logix` | pycomm3 | ✔ | ✔ | ✔ | Planned-Full | Via 1756-EN2T/EN2TR/EN3TR; path `IP/backplane_slot` (e.g. `10.0.0.5/0`). FW v20+ expected; verify. |
| Rockwell ControlLogix L8x | EtherNet/IP CIP | `rockwell_logix` | pycomm3 | ✔ | ✔ | ✔ | Planned-Full | Embedded or module Ethernet. **CIP Security must not be enforced** on the path used (pycomm3 has no TLS). |
| CompactLogix 5370/5380/5480 | EtherNet/IP CIP | `rockwell_logix` | pycomm3 | ✔ | ✔ | ✔ | Planned-Full | Path = IP only, slot 0. |
| Micro800 | EtherNet/IP CIP | `rockwell_logix` | pycomm3 | Partial | Partial | Partial | Planned-Partial | No program-scoped tags; limited types. Not in initial validation scope. |
| PLC-5 / SLC-500 / MicroLogix | PCCC | — | — | ✘ | ✘ | ✘ | Not supported | Not requested. pycomm3 `SLCDriver` could be added later. |
| Mitsubishi FX5U / FX5UC | SLMP = MC 3E binary (TCP) | `mitsubishi_mc` | own `mc_protocol.py` | ✔ | ✔ | ✔ | Planned-Full | Built-in Ethernet; an SLMP/MC connection must be configured in GX Works3 (port, TCP, binary, "enable write during RUN" for writes). |
| Mitsubishi FX3U/FX3UC + FX3U-ENET-ADP | MC 1E binary (TCP) | `mitsubishi_mc` | own `mc_protocol.py` | ✔ | ✔ (if permitted) | ✔ (batch per device type) | Planned-Full (frame) / HW validation critical | 1E frame only; small batch sizes; configured with FX Configurator-EN / GX Works2. Write-in-RUN setting required for writes. |
| Mitsubishi FX3U + FX3U-ENET(-L) | MC 1E binary | `mitsubishi_mc` | own | ✔ | ✔ | ✔ | Planned-Full | As above. |
| Mitsubishi FX3U serial (computer link / programming port) | Mitsubishi dedicated protocol | — | — | ✘ | ✘ | ✘ | **Not supported (initial)** | Use FX3U-485ADP-MB + Modbus RTU, or add ENET adapter. |
| Mitsubishi Q / L / iQ-R | MC 3E binary | `mitsubishi_mc` | own | ✔ | ✔ | ✔ | Planned-Full | Not in spec but supported by 3E framing; iQ-R uses extended device codes (handled). |
| Modbus TCP devices | Modbus TCP | `modbus_tcp` | pymodbus | ✔ FC1/2/3/4 | ✔ FC5/6/15/16 | ✔ block coalescing | Planned-Full | Per-device byte/word order. Max 125 regs/read, 2000 coils/read. |
| Modbus RTU (RS485/RS232) | Modbus RTU | `modbus_rtu` | pymodbus + pyserial | ✔ | ✔ | ✔ | Planned-Full | One serial bus = one DeviceWorker; multiple slaves on one bus share it (serialized). Broadcast writes not supported. |
| OPC UA servers (any vendor) | OPC UA binary | `opc_ua` | asyncua | ✔ | ✔ | ✔ | Planned-Full | Security policies Basic256Sha256 / Aes128 / Aes256; certificate trust must be established on both sides. Basic128Rsa15/Basic256 (deprecated) rejected by default. |
| OPC DA servers (e.g. legacy Kepware, RSLinx) | OPC DA 2.0 via COM | `opc_da` | pywin32 + OPC DA Automation Wrapper | ✔ | ✔ | ✔ | Planned-Partial | **Windows only.** Requires a registered OPC DA Automation wrapper DLL (`OPCDAAuto.dll`, often 32-bit only → 32-bit Python for the bridge). Remote DCOM is fragile; local server recommended. Never available on macOS. |

---

## 3. Siemens specifics (summary — full procedure in PLC_CONFIGURATION.md, Phase 8)

1. **PUT/GET:** CPU properties → *Protection & Security* → *Connection mechanisms* →
   "Permit access with PUT/GET communication from remote partner". This is a security
   reduction; the gateway never enables it — it is an explicit engineering decision.
2. **Non-optimized DBs:** DB properties → *Attributes* → untick "Optimized block access",
   compile, download. Offsets then come from the DB's "Offset" column.
3. **Access level:** S7-1200/1500 FW ≥ V4.x (1200) / V2.x (1500): full access or a level
   that allows HMI-equivalent access. Newer firmware (S7-1200 G2, S7-1500 FW ≥ V3.1 with
   "secure PG/HMI communication") may restrict or disable legacy PUT/GET — **must be
   verified per CPU and firmware**.
4. **Rack/slot:** S7-1200/1500: 0/1. S7-300: 0/2. Always verify against device configuration.
5. **Data types:** S7-1200/1500 support LREAL, LINT, ULINT etc.; S7-300 does not.
   STRING has a 2-byte header (max length, actual length).

---

## 4. What must happen before claiming "supported"

For each model in the field, the commissioning checklist (Phase 8) records:

- CPU order number and firmware version
- Communication module and firmware
- Protocol settings applied (PUT/GET, MC open settings, CIP path, OPC UA security)
- Hardware smoke test result (`pytest -m hardware`): connect, read every type in use,
  read 100+ tags batched, disconnect/reconnect, optional test write on a scratch tag
- 24-hour soak read-only run with zero unexplained BAD qualities

Only then is the matrix row marked **HW-Verified** with the date and firmware.

---

## 5. Findings during implementation (2026-11-12)

| Finding | Impact | Action |
|---|---|---|
| PyMySQL 1.2.x breaks aiomysql 0.3.2 for `bytes` parameters | All history/event inserts would fail in production | Pinned PyMySQL 1.1.2; covered by MySQL tests |
| python-snap7 3.5 `Client.connect()` uses a fixed 5 s socket timeout | Configured `timeout_s` was ignored | Driver applies `timeout_s` to the connection after connect |
| python-snap7's bundled `snap7.Server` ignores the bit index of BIT writes (writes the whole byte) | Simulator is not a faithful model for bit writes | Driver request encoding verified by unit test (transport size BIT, address = byte·8+bit); **bit writes must be verified on a real CPU** |
| Python 3.11 `asyncio.Server.close()` keeps accepted connections open | Simulators kept answering after "stop" | Simulators close their connections explicitly |
| pymodbus 3.16 datastore (`ModbusDeviceContext`) is deprecated pending v4 | Unstable simulator API | Independent Modbus TCP simulator written from the spec |
| MySQL `VALUES()` in `ON DUPLICATE KEY UPDATE` deprecated (still supported in 8.4 LTS) | Future MySQL major versions | Kept deliberately (PyMySQL batching); see ARCHITECTURE D19 |
| Rockwell Logix, Modbus RTU and OPC DA have no simulation coverage | Untested code paths beyond unit level | Hardware smoke tests (`tests/hardware`) mandatory at commissioning |
