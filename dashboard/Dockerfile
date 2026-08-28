FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY dashboard/dashboard.py /app/
COPY dashboard/static /app/static

# Run as the same user the squid helpers use (Debian's `proxy`, uid 13) so the
# SQLite database and its -wal/-shm files on the shared /config volume stay
# writable by both containers. A fresh named volume first mounted here is
# seeded with this ownership.
RUN mkdir -p /config && chown -R proxy:proxy /config /app
USER proxy

EXPOSE 8787

CMD ["python3", "dashboard.py"]
