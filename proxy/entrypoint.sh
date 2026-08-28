#!/bin/sh
set -eu

CONFIG_DIR=/config
SSL_DIR="$CONFIG_DIR/ssl_cert"
mkdir -p "$CONFIG_DIR" "$SSL_DIR"

export PP_DB_PATH="$CONFIG_DIR/parental_proxy.db"

# Seed defaults (idempotent -- safe to run every start, changes nothing on
# an already-configured database). Also bootstraps local_network from env
# on first run only.
python3 /opt/parental-proxy/defaults/seed_defaults.py
python3 - << PYEOF
import sys
sys.path.insert(0, "/opt/parental-proxy")
import db
conn = db.get_conn()
db.init_db(conn)
db.set_setting_if_absent(conn, "local_network", "${LOCAL_NETWORK:-192.168.1.0/24}")
db.set_setting_if_absent(conn, "block_page_mode", "terminate")
conn.commit()
PYEOF

# Generate the SSL-bump CA certificate on first run. This is what gets
# installed as a trusted root on client devices -- the private key never
# leaves this container.
if [ ! -f "$SSL_DIR/ca_cert.pem" ] || [ ! -f "$SSL_DIR/ca_key.pem" ]; then
  echo "Generating a new SSL-bump CA certificate..."
  openssl req -new -newkey rsa:2048 -sha256 -days 3650 -nodes -x509 \
    -keyout "$SSL_DIR/ca_key.pem" \
    -out "$SSL_DIR/ca_cert.pem" \
    -subj "/O=${CA_ORG:-Parental Proxy}/CN=${CA_COMMON_NAME:-Parental Proxy CA}"
fi

# Initialize Squid's SSL certificate cache database if this is the first run.
if [ ! -d /var/lib/squid/ssl_db ]; then
  /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB
fi
chown -R proxy:proxy /var/lib/squid/ssl_db "$SSL_DIR" "$CONFIG_DIR" 2>/dev/null || true

# Render squid.conf, then conditionally append the friendly-block-page
# redirect if DASHBOARD_URL is configured.
cp /etc/squid/squid.conf.template /etc/squid/squid.conf
if [ -n "${DASHBOARD_URL:-}" ]; then
  echo "deny_info ${DASHBOARD_URL}/blocked authz_allowed" >> /etc/squid/squid.conf
fi

exec squid -N -f /etc/squid/squid.conf
