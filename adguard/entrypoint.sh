#!/bin/sh
set -eu

# Wraps the official adguard/adguardhome image's own entrypoint
# (/opt/adguardhome/AdGuardHome) so this container comes up fully
# configured on first boot -- no manual setup-wizard step, matching
# proxy/entrypoint.sh's own idempotent-bootstrap pattern for the CA
# cert. AdGuardHome.yaml (on the persisted /opt/adguardhome/conf
# volume) existing at all is what AdGuard itself uses to decide whether
# it's already configured -- once it exists, every later start is a
# plain, unmodified launch.

CONF=/opt/adguardhome/conf/AdGuardHome.yaml
BIN=/opt/adguardhome/AdGuardHome
WORK=/opt/adguardhome/work

if [ -f "$CONF" ]; then
  # Already configured from a previous run (persisted volume) --
  # nothing to bootstrap. exec so this process IS pid 1 and receives
  # signals directly, same as the unwrapped image would.
  exec "$BIN" --no-check-update -c "$CONF" -w "$WORK"
fi

if [ -z "${ADGUARD_PASSWORD:-}" ]; then
  # Mirrors dashboard/dashboard.py's bootstrap_admin() exactly -- same
  # message shape, same "generate and print once, editable afterward"
  # behavior -- for the same reason: no default admin password should
  # ever ship, but failing to boot at all over a missing one would be
  # worse than a random one the operator can rotate right after.
  ADGUARD_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20)
  SEP=$(printf '=%.0s' $(seq 1 64))
  {
    echo ""
    echo "$SEP"
    echo "  No ADGUARD_PASSWORD was set. Generated an admin login:"
    echo "    username: ${ADGUARD_USERNAME:-admin}"
    echo "    password: $ADGUARD_PASSWORD"
    echo "  Change it from the Settings page after logging in."
    echo "$SEP"
    echo ""
  } >&2
fi

echo "First run: bootstrapping AdGuard Home via its own install API (no manual wizard)..." >&2

# Backgrounded (not exec'd) only for this first-run path, specifically
# so this script can poll it and complete setup via its own real HTTP
# API before handing off control -- confirmed live 2026-08-30 against a
# real v0.107.79 instance that /control/install/configure (NOT the bare
# /install/configure some of AdGuard's own generated OpenAPI-doc
# tooling implies -- every route lives under /control, even before the
# instance is configured at all) writes a complete, correctly-versioned
# AdGuardHome.yaml itself. Hand-authoring that file from the wiki's
# documented fields was tried first and found missing several fields
# the real binary always writes (session_ttl's duration format,
# upstream_mode, cache_optimistic_*, the doh.routes block) -- letting
# AdGuard build its own config is both simpler and impossible to drift
# out of sync with whatever version is actually running.
"$BIN" --no-check-update -c "$CONF" -w "$WORK" &
PID=$!
trap 'kill -TERM "$PID" 2>/dev/null; wait "$PID" 2>/dev/null' TERM INT

i=0
while ! wget -q -O /dev/null http://127.0.0.1:3000/control/install/get_addresses 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "AdGuard Home did not come up for setup within 30s" >&2
    kill -TERM "$PID" 2>/dev/null
    wait "$PID" 2>/dev/null
    exit 1
  fi
  sleep 1
done

# Minimal JSON-string escaping (backslash, then double-quote -- the two
# characters that matter for a plain JSON string value) so an
# operator-supplied ADGUARD_USERNAME/ADGUARD_PASSWORD containing either
# doesn't break the request body. Web port is always left at the
# image's own default (3000) -- only DNS gets a non-default port
# (5353, matching phase3/nftables-manager's baselineRules redirect
# target) -- so this script never has to guess which port to poll
# above, and there's no live web-port change to verify (DNS's port DID
# need confirming: the running process picks up the new DNS port
# immediately after configure, live, with no restart -- confirmed
# 2026-08-30, real dig queries succeeded against :5353 within the same
# process that was still only listening on :3000/HTTP moments earlier).
_json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}
USERNAME_JSON=$(_json_escape "${ADGUARD_USERNAME:-admin}")
PASSWORD_JSON=$(_json_escape "$ADGUARD_PASSWORD")

# Web is ALWAYS configured onto the wildcard address here, regardless
# of ADGUARD_WEB_BIND -- confirmed live 2026-08-30 that
# /control/install/configure validates the NEW address by test-binding
# it before releasing the current pre-configure listener, and asking
# for exactly the same specific address that listener already holds
# (e.g. 127.0.0.1:3000) fails with "address already in use" against
# itself; the wildcard doesn't conflict the same way (standard Linux
# bind() behavior: a wildcard bind coexists with an already-bound
# specific address, where repeating that exact specific address does
# not). ADGUARD_WEB_BIND is applied as a second step below instead.
wget -q -O /dev/null \
  --header 'Content-Type: application/json' \
  --post-data "{\"web\":{\"ip\":\"0.0.0.0\",\"port\":3000},\"dns\":{\"ip\":\"0.0.0.0\",\"port\":${ADGUARD_DNS_PORT:-5353}},\"username\":\"$USERNAME_JSON\",\"password\":\"$PASSWORD_JSON\"}" \
  http://127.0.0.1:3000/control/install/configure

if [ ! -f "$CONF" ]; then
  echo "AdGuard Home install/configure did not produce $CONF -- aborting" >&2
  kill -TERM "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
  exit 1
fi

# Same reasoning as dashboard/dashboard.py's DASHBOARD_BIND default:
# with `network_mode: host` (required for DNS interception, see
# docker-compose.yml's own comment), the wildcard bind above would put
# AdGuard Home's own admin UI -- a second login surface this project
# didn't build, separate from our dashboard -- directly on the LAN. If
# a non-wildcard bind was actually requested, rewrite the now-written
# config's http.address directly and restart onto it: AdGuardHome.yaml
# only takes effect while the process isn't running (AdGuard's own
# documented behavior), which this restart satisfies, and there's no
# more self-conflict once the wildcard listener above is torn down
# first. Set ADGUARD_WEB_BIND=0.0.0.0 to skip this step and deliberately
# expose it on the LAN instead.
WEB_BIND="${ADGUARD_WEB_BIND:-127.0.0.1}"
if [ "$WEB_BIND" != "0.0.0.0" ]; then
  kill -TERM "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
  trap - TERM INT
  sed -i "s/^  address: 0\.0\.0\.0:3000\$/  address: ${WEB_BIND}:3000/" "$CONF"
  exec "$BIN" --no-check-update -c "$CONF" -w "$WORK"
fi

echo "AdGuard Home configured (DNS on :${ADGUARD_DNS_PORT:-5353}, admin UI on 0.0.0.0:3000)." >&2
wait "$PID"
