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
    && rm -rf /var/lib/apt/lists/*

# Pre-create /config owned by the `proxy` user (uid 13 on Debian). If a fresh
# named volume is first initialised by this container, Docker seeds it with
# this ownership so the squid helper processes (which run as `proxy`) can
# write the SQLite database. entrypoint.sh also re-chowns it on every start
# to cover volumes created by the dashboard container.
RUN mkdir -p /opt/parental-proxy/defaults /config \
    && chown -R proxy:proxy /config

COPY common/*.py /opt/parental-proxy/
COPY proxy/basic_auth_helper.py proxy/sni_helper.py proxy/authz_helper.py /opt/parental-proxy/
COPY defaults/seed_defaults.py /opt/parental-proxy/defaults/
COPY proxy/squid.conf.template /etc/squid/squid.conf.template
COPY proxy/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh /opt/parental-proxy/*.py /opt/parental-proxy/defaults/*.py

EXPOSE 3128

ENTRYPOINT ["/entrypoint.sh"]
