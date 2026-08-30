# Milestone 3's controller, containerized 2026-08-30 -- previously only
# ever run by hand during verification passes (see this file's own
# module docstrings for the history). Flat-copies common/*.py alongside
# controller/*.py into one directory, same pattern as proxy/Dockerfile
# and dashboard/Dockerfile use for common/ -- main.py's own sys.path
# bootstrap handles both this flat layout and a real repo checkout.
FROM python:3.12-slim

WORKDIR /app

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY controller/*.py /app/

ENTRYPOINT ["python3", "main.py"]
