FROM debian:bookworm-slim

# squid-openssl, not squid: Debian's default `squid` package is built
# --with-gnutls, which does not support SSL-Bump at all (on-the-fly leaf
# certificate signing needs OpenSSL) -- `squid -k parse` fails with "Unknown
# http_port option 'ssl-bump'" on the plain package. squid-openssl is the
# same version, built --with-openssl instead, and is what this project's
# entire ssl_bump config depends on.
RUN apt-get update && apt-get install -y --no-install-recommends \
        squid-openssl \
        openssl \
        python3 \
        ca-certificates \
        libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Intercept mode's IP_TRANSPARENT bind + SO_ORIGINAL_DST lookup need
# CAP_NET_ADMIN -- but this Debian build of Squid is compiled
# --with-default-user=proxy, so it always calls setuid() down to the
# unprivileged `proxy` user during startup (see tools.cc's leave_suid,
# visible in a `squid -X` trace as "giving up root, becoming 'proxy'")
# *before* it gets to opening those listener sockets. Linux drops a
# process's capabilities on setuid() unless they're preserved another
# way, so cap_add: NET_ADMIN on the container alone isn't enough --
# confirmed live 2026-08-30, where this crashed Squid silently
# (RegisteredRunner::finalizeConfig never completing, no message at
# the default log level) despite the container capability being
# present the whole time. A file capability survives setuid() because
# it's granted to the binary itself, not the process's inherited set --
# this is Squid's own documented fix for TPROXY/intercept deployments
# that don't run as root.
RUN setcap 'cap_net_admin=+ep' /usr/sbin/squid

# Pre-create /config owned by the `proxy` user (uid 13 on Debian). If a fresh
# named volume is first initialised by this container, Docker seeds it with
# this ownership so the squid helper processes (which run as `proxy`) can
# write the SQLite database. entrypoint.sh also re-chowns it on every start
# to cover volumes created by the dashboard container.
RUN mkdir -p /opt/parental-proxy/defaults /config \
    && chown -R proxy:proxy /config

COPY common/*.py /opt/parental-proxy/
COPY proxy/sni_helper.py proxy/authz_helper.py /opt/parental-proxy/
COPY defaults/seed_defaults.py /opt/parental-proxy/defaults/
COPY proxy/squid.conf.template /etc/squid/squid.conf.template
COPY proxy/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh /opt/parental-proxy/*.py /opt/parental-proxy/defaults/*.py

# Intercept-mode ports (RoadMap.md's Squid intercept-mode section, locked
# 2026-08-30) -- nftables NAT-redirects a bump_v4 device's own port 80/443
# traffic here, replacing the old explicit-proxy 3128.
EXPOSE 3129 3130

ENTRYPOINT ["/entrypoint.sh"]
