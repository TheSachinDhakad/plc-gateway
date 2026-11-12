-- Least-privilege MySQL users. Run as an administrator AFTER replacing the placeholders.
-- Not applied automatically. Restrict hosts to the gateway IPC / Grafana server addresses.

-- Gateway application user
CREATE USER IF NOT EXISTS 'gw_app'@'10.10.20.%' IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';
GRANT SELECT ON plc_gateway.* TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plants TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_devices TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_tags TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_tag_history TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_tag_latest TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_events TO 'gw_app'@'10.10.20.%';
GRANT INSERT, UPDATE ON plc_gateway.plc_write_commands TO 'gw_app'@'10.10.20.%';
GRANT INSERT, DELETE ON plc_gateway.gateway_health TO 'gw_app'@'10.10.20.%';
GRANT INSERT ON plc_gateway.audit_logs TO 'gw_app'@'10.10.20.%';      -- append-only
-- partition maintenance (create ahead / drop expired) on partitioned tables
GRANT ALTER, DROP ON plc_gateway.plc_tag_history TO 'gw_app'@'10.10.20.%';
GRANT ALTER, DROP ON plc_gateway.plc_events TO 'gw_app'@'10.10.20.%';

-- Optional maintenance user for audit retention (MYSQL_MAINT_USER)
CREATE USER IF NOT EXISTS 'gw_maint'@'10.10.20.%' IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';
GRANT SELECT, DELETE ON plc_gateway.audit_logs TO 'gw_maint'@'10.10.20.%';

-- Grafana read-only user
CREATE USER IF NOT EXISTS 'gw_grafana'@'10.10.30.%' IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';
GRANT SELECT ON plc_gateway.v_tags TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.v_tag_latest TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.v_tag_history TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.v_events TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.gateway_health TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.plc_tag_history TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.plc_tags TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.plc_devices TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.plants TO 'gw_grafana'@'10.10.30.%';
GRANT SELECT ON plc_gateway.quality_codes TO 'gw_grafana'@'10.10.30.%';

FLUSH PRIVILEGES;
