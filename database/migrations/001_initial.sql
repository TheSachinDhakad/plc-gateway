-- industrial-plc-gateway schema v1 (MySQL 8.0.19+ / 8.4 LTS)
-- All timestamps are UTC (sessions run with time_zone = '+00:00').
-- Partitioned tables (plc_tag_history, plc_events) cannot have foreign keys (MySQL limitation);
-- integrity for them is enforced by the gateway writer.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INT UNSIGNED NOT NULL PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    applied_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS quality_codes (
    quality     TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    name        VARCHAR(32) NOT NULL,
    is_good     TINYINT  NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO quality_codes (quality, name, is_good) VALUES
    (0, 'GOOD', 1), (1, 'UNCERTAIN', 0), (2, 'BAD', 0), (3, 'BAD_NOT_CONNECTED', 0),
    (4, 'BAD_COMM_FAILURE', 0), (5, 'BAD_TIMEOUT', 0), (6, 'BAD_ADDRESS', 0),
    (7, 'BAD_TYPE_MISMATCH', 0), (8, 'BAD_CONFIG_ERROR', 0), (9, 'BAD_DEVICE_FAILURE', 0)
ON DUPLICATE KEY UPDATE name = VALUES(name), is_good = VALUES(is_good);

CREATE TABLE IF NOT EXISTS value_type_codes (
    value_type  TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    name        VARCHAR(16) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO value_type_codes (value_type, name) VALUES
    (0, 'null'), (1, 'bool'), (2, 'int'), (3, 'float'), (4, 'str'), (5, 'array')
ON DUPLICATE KEY UPDATE name = VALUES(name);

CREATE TABLE IF NOT EXISTS plants (
    plant_pk    INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    plant_id    VARCHAR(64)  NOT NULL,
    name        VARCHAR(128) NOT NULL,
    enabled     TINYINT   NOT NULL DEFAULT 1,
    created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_plants_plant_id (plant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS plc_devices (
    device_pk           INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    plant_pk            INT UNSIGNED NOT NULL,
    device_id           VARCHAR(64)  NOT NULL,
    name                VARCHAR(128) NOT NULL,
    driver              VARCHAR(32)  NOT NULL,
    enabled             TINYINT   NOT NULL DEFAULT 1,
    write_enabled       TINYINT   NOT NULL DEFAULT 0,
    endpoint            VARCHAR(255) NULL COMMENT 'host/endpoint only, never credentials',
    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_devices_device_id (device_id),
    KEY ix_devices_plant (plant_pk),
    CONSTRAINT fk_devices_plant FOREIGN KEY (plant_pk) REFERENCES plants (plant_pk)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS plc_tags (
    tag_pk              INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    device_pk           INT UNSIGNED NOT NULL,
    tag_id              VARCHAR(64)  NOT NULL,
    name                VARCHAR(128) NOT NULL,
    description         VARCHAR(512) NULL,
    tag_group           VARCHAR(64)  NULL,
    address             VARCHAR(256) NOT NULL,
    data_type           VARCHAR(16)  NOT NULL,
    array_length        SMALLINT UNSIGNED NULL,
    poll_interval_ms    INT UNSIGNED NOT NULL,
    logging_mode        VARCHAR(8)   NOT NULL,
    engineering_unit    VARCHAR(32)  NULL,
    access_mode         VARCHAR(16)  NOT NULL DEFAULT 'read_only',
    enabled             TINYINT   NOT NULL DEFAULT 1,
    config_hash         CHAR(64)     NOT NULL,
    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_tags_device_tag (device_pk, tag_id),
    KEY ix_tags_tag_id (tag_id),
    CONSTRAINT fk_tags_device FOREIGN KEY (device_pk) REFERENCES plc_devices (device_pk)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Time series. One row per stored reading. Typed value columns (no blind DOUBLE).
-- PK clusters rows by tag then time -> efficient per-tag range scans for Grafana/API.
-- reading_uid (UUIDv7) makes spool replay idempotent.
CREATE TABLE IF NOT EXISTS plc_tag_history (
    tag_pk          INT UNSIGNED      NOT NULL,
    gateway_ts      DATETIME(6)       NOT NULL,
    reading_uid     BINARY(16)        NOT NULL,
    source_ts       DATETIME(6)       NULL,
    ts_origin       TINYINT UNSIGNED  NOT NULL COMMENT '0=gateway_read 1=device',
    value_type      TINYINT UNSIGNED  NOT NULL,
    value_bool      TINYINT        NULL,
    value_int       BIGINT            NULL,
    value_float     DOUBLE            NULL,
    value_str       VARCHAR(1024)     NULL,
    value_json      JSON              NULL,
    quality         TINYINT UNSIGNED  NOT NULL,
    error_code      VARCHAR(64)       NULL,
    PRIMARY KEY (tag_pk, gateway_ts, reading_uid),
    KEY ix_history_ts (gateway_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
PARTITION BY RANGE COLUMNS (gateway_ts) (
    PARTITION p_initial VALUES LESS THAN ('2000-01-01'),
    PARTITION p_future  VALUES LESS THAN (MAXVALUE)
);

CREATE TABLE IF NOT EXISTS plc_tag_latest (
    tag_pk          INT UNSIGNED      NOT NULL PRIMARY KEY,
    gateway_ts      DATETIME(6)       NOT NULL,
    source_ts       DATETIME(6)       NULL,
    ts_origin       TINYINT UNSIGNED  NOT NULL,
    value_type      TINYINT UNSIGNED  NOT NULL,
    value_bool      TINYINT        NULL,
    value_int       BIGINT            NULL,
    value_float     DOUBLE            NULL,
    value_str       VARCHAR(1024)     NULL,
    value_json      JSON              NULL,
    quality         TINYINT UNSIGNED  NOT NULL,
    error_code      VARCHAR(64)       NULL,
    CONSTRAINT fk_latest_tag FOREIGN KEY (tag_pk) REFERENCES plc_tags (tag_pk)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS plc_events (
    event_ts            DATETIME(6)   NOT NULL,
    event_uid           BINARY(16)    NOT NULL,
    device_pk           INT UNSIGNED  NOT NULL,
    tag_pk              INT UNSIGNED  NULL COMMENT 'NULL for device-level events',
    event_type          VARCHAR(24)   NOT NULL,
    previous_value      JSON          NULL,
    new_value           JSON          NULL,
    previous_quality    VARCHAR(24)   NULL,
    new_quality         VARCHAR(24)   NULL,
    threshold           DOUBLE        NULL,
    message             VARCHAR(512)  NULL,
    PRIMARY KEY (event_ts, event_uid),
    KEY ix_events_tag_ts (tag_pk, event_ts),
    KEY ix_events_device_ts (device_pk, event_ts),
    KEY ix_events_type_ts (event_type, event_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
PARTITION BY RANGE COLUMNS (event_ts) (
    PARTITION p_initial VALUES LESS THAN ('2000-01-01'),
    PARTITION p_future  VALUES LESS THAN (MAXVALUE)
);

CREATE TABLE IF NOT EXISTS plc_write_commands (
    command_pk          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    request_id          VARCHAR(64)   NOT NULL,
    plant_id            VARCHAR(64)   NOT NULL,
    device_id           VARCHAR(64)   NOT NULL,
    tag_id              VARCHAR(64)   NOT NULL,
    principal           VARCHAR(128)  NOT NULL,
    requested_value     JSON          NULL,
    readback_value      JSON          NULL,
    reason              VARCHAR(512)  NULL,
    status              VARCHAR(16)   NOT NULL,
    error_code          VARCHAR(64)   NULL,
    error_message       VARCHAR(1024) NULL,
    requested_at        DATETIME(6)   NOT NULL,
    completed_at        DATETIME(6)   NULL,
    duration_ms         DOUBLE        NULL,
    UNIQUE KEY uq_write_request_id (request_id),
    KEY ix_write_device_tag_ts (device_id, tag_id, requested_at),
    KEY ix_write_ts (requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS gateway_health (
    snapshot_ts         DATETIME(6)   NOT NULL,
    gateway_id          VARCHAR(64)   NOT NULL,
    device_id           VARCHAR(64)   NOT NULL COMMENT '_gateway for process-level rows',
    status              VARCHAR(16)   NOT NULL,
    connected           TINYINT    NULL,
    reads_ok            BIGINT UNSIGNED NULL,
    reads_failed        BIGINT UNSIGNED NULL,
    reconnects          INT UNSIGNED  NULL,
    poll_p95_ms         DOUBLE        NULL,
    queue_size          INT UNSIGNED  NULL,
    spool_rows          BIGINT UNSIGNED NULL,
    dropped_total       BIGINT UNSIGNED NULL,
    db_insert_p95_ms    DOUBLE        NULL,
    cpu_percent         DOUBLE        NULL,
    rss_mb              DOUBLE        NULL,
    detail              JSON          NULL,
    PRIMARY KEY (snapshot_ts, gateway_id, device_id),
    KEY ix_health_device_ts (device_id, snapshot_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_pk            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts                  DATETIME(6)   NOT NULL,
    gateway_id          VARCHAR(64)   NOT NULL,
    action              VARCHAR(64)   NOT NULL,
    principal           VARCHAR(128)  NULL,
    result              VARCHAR(32)   NOT NULL,
    device_id           VARCHAR(64)   NULL,
    tag_id              VARCHAR(64)   NULL,
    request_id          VARCHAR(64)   NULL,
    detail              JSON          NULL,
    KEY ix_audit_ts (ts),
    KEY ix_audit_principal_ts (principal, ts),
    KEY ix_audit_action_ts (action, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Grafana-friendly views (read-only user gets SELECT on these)
CREATE OR REPLACE VIEW v_tags AS
SELECT t.tag_pk, p.plant_id, d.device_id, d.name AS device_name, t.tag_id, t.name AS tag_name,
       t.tag_group, t.data_type, t.engineering_unit, t.logging_mode, t.enabled
FROM plc_tags t
JOIN plc_devices d ON d.device_pk = t.device_pk
JOIN plants p ON p.plant_pk = d.plant_pk;

CREATE OR REPLACE VIEW v_tag_latest AS
SELECT v.plant_id, v.device_id, v.device_name, v.tag_id, v.tag_name, v.engineering_unit,
       l.gateway_ts, l.source_ts,
       COALESCE(l.value_float, l.value_int, l.value_bool) AS value_num,
       l.value_str, l.value_json, q.name AS quality, q.is_good
FROM plc_tag_latest l
JOIN v_tags v ON v.tag_pk = l.tag_pk
JOIN quality_codes q ON q.quality = l.quality;

CREATE OR REPLACE VIEW v_tag_history AS
SELECT h.gateway_ts AS time, v.plant_id, v.device_id, v.tag_id, v.tag_name, v.engineering_unit,
       COALESCE(h.value_float, h.value_int, h.value_bool) AS value_num,
       h.value_str, h.quality, h.tag_pk
FROM plc_tag_history h
JOIN v_tags v ON v.tag_pk = h.tag_pk;

CREATE OR REPLACE VIEW v_events AS
SELECT e.event_ts AS time, d.device_id, t.tag_id, e.event_type, e.previous_value, e.new_value,
       e.previous_quality, e.new_quality, e.threshold, e.message
FROM plc_events e
JOIN plc_devices d ON d.device_pk = e.device_pk
LEFT JOIN plc_tags t ON t.tag_pk = e.tag_pk;

INSERT INTO schema_migrations (version, name) VALUES (1, '001_initial')
ON DUPLICATE KEY UPDATE name = VALUES(name);
