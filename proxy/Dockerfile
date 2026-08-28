FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        squid \
        openssl \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/parental-proxy/defaults

COPY common/*.py /opt/parental-proxy/
COPY proxy/basic_auth_helper.py proxy/sni_helper.py proxy/authz_helper.py /opt/parental-proxy/
COPY defaults/seed_defaults.py /opt/parental-proxy/defaults/
COPY proxy/squid.conf.template /etc/squid/squid.conf.template
COPY proxy/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh /opt/parental-proxy/*.py /opt/parental-proxy/defaults/*.py

EXPOSE 3128

ENTRYPOINT ["/entrypoint.sh"]
