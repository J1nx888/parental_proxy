# Milestone 3's controller, containerized 2026-08-30 -- previously only
# ever run by hand during verification passes (see this file's own
# module docstrings for the history). Flat-copies common/*.py alongside
# controller/*.py into one directory, same pattern as proxy/Dockerfile
# and dashboard/Dockerfile use for common/ -- main.py's own sys.path
# bootstrap handles both this flat layout and a real repo checkout.
FROM python:3.12-slim

# iproute2 -- controller/discovery.py's snapshot_once() shells out to
# the real `ip neigh show` command; python:3.12-slim doesn't ship it.
# Confirmed live 2026-08-30 (this container's first real run): the
# discovery loop failed every single cycle with "[Errno 2] No such file
# or directory: 'ip'", silently (discovery.run_loop's own on_error just
# logs a warning and keeps retrying, so this never crashed the
# container -- it just never actually discovered anything).
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY controller/*.py /app/

ENTRYPOINT ["python3", "main.py"]
