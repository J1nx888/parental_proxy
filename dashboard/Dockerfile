FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY dashboard/dashboard.py dashboard/block_page_server.py /app/
COPY dashboard/static /app/static

# Run as the same user the squid helpers use (Debian's `proxy`, uid 13) so the
# SQLite database and its -wal/-shm files on the shared /config volume stay
# writable by both containers. A fresh named volume first mounted here is
# seeded with this ownership.
RUN mkdir -p /config && chown -R proxy:proxy /config /app
USER proxy

# Port 80 (block_page_server.py, started only when DASHBOARD_URL is set)
# needs CAP_NET_BIND_SERVICE granted at the container level -- this
# non-root `proxy` user can't bind a privileged port on its own. See
# docker-compose.yml's own comment.
EXPOSE 8787 80

CMD ["python3", "dashboard.py"]
