# PLC configuration guide

Every change below is made by the PLC engineer in the PLC engineering tool, following plant
change management. The gateway never changes PLC settings.

## 1. Siemens S7-1200 / S7-1500 (driver `siemens_s7`)

The driver uses classic S7 communication (ISO-on-TCP, TCP 102) with absolute addresses.

### 1.1 Enable PUT/GET (TIA Portal)

1. Device view → select the CPU → **Properties** → **Protection & Security** → **Connection mechanisms**.
2. Tick **"Permit access with PUT/GET communication from remote partner"**.
3. Compile and download the hardware configuration.

PUT/GET removes authentication for S7 data access; it is a deliberate security trade-off. Keep the
CPU in a segmented OT cell (see SECURITY.md). Where possible, put gateway-written data in a
separate DB that PLC logic validates.

### 1.2 Non-optimized data blocks

Absolute addresses such as `DB100.DBD0` only work if the DB has fixed offsets:

1. Project tree → right-click the DB (e.g. DB100) → **Properties** → **Attributes**.
2. Untick **"Optimized block access"** → OK → compile → download (this reinitializes the DB; plan it).
3. Open the DB: the **Offset** column now shows byte offsets — use these in `tags.yaml`.

| DB variable | Offset | Gateway address | data_type |
|---|---|---|---|
| Temperature : Real | 0.0 | `DB100.DBD0` | REAL |
| Pressure : Real | 4.0 | `DB100.DBD4` | REAL |
| Counter : Int | 8.0 | `DB100.DBW8` | INT |
| MotorStatus : Bool | 10.0 | `DB100.DBX10.0` | BOOL |
| TestWriteValue : Real | 12.0 | `DB100.DBD12` | REAL |
| Text : String[20] | 14.0 | `DB100.DBB14` + `string_length: 20` | STRING |

### 1.3 Rack / slot / access level

- S7-1200 / S7-1500: rack 0, slot 1. S7-300: rack 0, slot 2. S7-400: slot of the CPU in the rack.
  Always confirm in Device configuration.
- **Protection level**: full access, or a level that still permits HMI-type access. Newer firmware
  (e.g. S7-1500 V3.1+ with "secure PG/PC and HMI communication only", S7-1200 G2) may block legacy
  PUT/GET entirely — verify per CPU and firmware with the hardware smoke test.
- `connection_type`: PG (default), OP or S7_BASIC — change only if the CPU's connection resources are exhausted.

### 1.4 Optimized DBs / symbolic access

Not possible over classic S7. Use the CPU's **OPC UA server** instead (S7-1500; S7-1200 FW ≥ V4.4,
runtime licence required): enable it in CPU properties → OPC UA → Server, export the server
certificate, and configure an `opc_ua` device (section 5).

### 1.5 Commissioning test

```
python scripts/validate_config.py
PLC_HW_CONFIG_DIR=config PLC_HW_DEVICE=siemens_01 pytest -m hardware tests/hardware -v -s
```
Bit writes (`DBX`) must be verified on the real CPU (the development simulator cannot model them).

## 2. Rockwell ControlLogix / CompactLogix (driver `rockwell_logix`)

- `path`: `IP/slot` — for ControlLogix the slot of the **controller** in the chassis behind the
  Ethernet module (e.g. `192.168.1.20/0`); for CompactLogix the IP only (`192.168.1.21`).
- Tags are symbolic: controller scope `Tag`, program scope `Program:MainProgram.Tag`, UDT members
  `Motor.Status`, array elements `Recipe[0]` with `array_length` for N elements.
- External access of the tag must be Read/Write (or Read Only for read-only tags).
- CIP Security must not be enforced on the path used (pycomm3 has no TLS support).
- Discover tags: `python scripts/discover_tags.py logix --device-id logix_01 --path 192.168.1.20/0`.
- Connection budget: each gateway device uses one CIP connection on the controller.

## 3. Mitsubishi (driver `mitsubishi_mc`)

### 3.1 FX5U (SLMP / MC 3E binary) — GX Works3

1. Parameter → FX5UCPU → Module Parameter → **Ethernet Port** → External Device Configuration.
2. Add **"SLMP Connection Module"**, protocol **TCP**, a port (e.g. 5000) — use this as `connection.port`.
3. Communication data code: **Binary**.
4. Only if writes are needed: enable **"Enable online change (write during RUN)"** for SLMP.
5. Write parameters to the PLC and reset.

### 3.2 FX3U + FX3U-ENET-ADP (MC 1E binary) — GX Works2 / FX Configurator-EN

1. Configure the adapter's IP and an **MC protocol** connection, TCP, **binary** code, port (e.g. 5551).
2. Allow **write during RUN** only if writes are required.
3. Gateway config: `frame: 1E`, `plc_series: FX3U`. 1E supports X, Y, M, S, TS, CS, D, R, TN, CN.

### 3.3 Addresses

- FX3U/FX5U: `X`/`Y` are **octal** (`X17` = 15th input); Q/L/iQ-R: `X/Y/B/W` are **hexadecimal**.
- 32-bit values use two words, low word at the lower address (`D200` DINT = D200 + D201).
- `D100.3` reads bit 3 of D100 (read-only; bit writes into word devices are rejected).
- Verify FX5U octal X/Y numbering and FX3U point limits with the hardware smoke test.

## 4. Modbus TCP / RTU (drivers `modbus_tcp`, `modbus_rtu`)

Addresses are **zero-based protocol addresses** with explicit table names:

| Device documentation | Gateway address |
|---|---|
| 40001 (holding register 1) | `holding:0` |
| 40101 | `holding:100` |
| 30001 (input register 1) | `input:0` |
| 00001 (coil 1) | `coil:0` |
| 10001 (discrete input 1) | `discrete:0` |
| bit 3 of holding register 40011 | `holding:10.3` (read-only) |

`4xxxx` notation is rejected on purpose. 32/64-bit values: set `byte_order` / `word_order` per
device (CDAB devices: `word_order: little`). RS485: one device entry per serial bus; address
slaves with `unit@` (e.g. `5@holding:0`). Typical RTU settings: 9600/19200 baud, 8E1 or 8N1.

## 5. OPC UA (driver `opc_ua`)

1. Create a client certificate (DER) and key (PEM) with application URI
   `urn:industrial-plc-gateway:client` (must match `application_uri`), e.g. with OpenSSL:
   ```
   openssl req -x509 -newkey rsa:2048 -keyout certs/gateway_client_key.pem -nodes \
     -out certs/gateway_client_cert.pem -days 1825 -subj "/CN=plc-gateway" \
     -addext "subjectAltName=URI:urn:industrial-plc-gateway:client"
   openssl x509 -in certs/gateway_client_cert.pem -outform der -out certs/gateway_client_cert.der
   ```
2. Trust the gateway certificate on the server (S7-1500: TIA Portal → certificate manager →
   trusted clients; or reject-then-trust on the server).
3. Export the server certificate to `certs/` and set `server_cert` (pinning).
4. `security_policy: Basic256Sha256` (or Aes128/Aes256), `security_mode: SignAndEncrypt`.
5. Optional user: `username_env` / `password_env` naming variables in `.env`.
6. Browse: `python scripts/discover_tags.py opcua --device-id opcua_01 --endpoint opc.tcp://… --client-cert … --client-key … --server-cert …`

## 6. OPC DA (driver `opc_da`, Windows only)

Requires the OPC DA server on the same PC (recommended) and a registered OPC Automation 2.0
wrapper (`OPCDAAuto.dll`). Many wrappers are 32-bit: then install a 32-bit Python with pywin32 and
set `connection.bridge_python` to it. Item IDs are the server's item names. Validate with the
hardware smoke test — this driver has no simulation coverage.
