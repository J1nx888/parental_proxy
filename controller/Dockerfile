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
#
# The sed below forces the base image's apt sources to HTTPS. Confirmed
# live 2026-09-01 on the smoke-test VM: plain `http://deb.debian.org`
# consistently failed apt's clearsign parser ("Clearsigned file isn't
# valid, got 'NOSPLIT'") while `curl`-fetching the exact same URL over
# plain HTTP from the VM's host network returned the identical
# (byte-for-byte diffed) file every time -- something between this
# Docker host's container network path and the mirror mangles the plain
# HTTP response specifically for apt's fetcher, not a corrupted upstream
# file. HTTPS to the same host is unaffected. Not investigated further
# (a Docker-layer or intermediate-network HTTP quirk, orthogonal to this
# project); routing apt around it is the pragmatic fix.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY controller/*.py /app/

ENTRYPOINT ["python3", "main.py"]
