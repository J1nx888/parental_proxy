FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/*.py /app/
COPY dashboard/dashboard.py /app/

EXPOSE 8787

CMD ["python3", "dashboard.py"]
