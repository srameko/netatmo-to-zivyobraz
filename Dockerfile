# ---- builder: install deps into an isolated directory ----
FROM python:3.16.0a1-alpine AS builder

WORKDIR /install
RUN pip install --no-cache-dir --target=/install requests==2.33.* "certifi>=2026.10.19" tzdata

# ---- final: clean Alpine Python, no pip, no cache ----
FROM python:3.16.0a1-alpine

RUN apk upgrade --no-cache

WORKDIR /app

COPY --from=builder /install /app/deps
COPY netatmo_to_zo.py .

ENV PYTHONPATH=/app/deps \
    PYTHONUNBUFFERED=1

RUN pip uninstall -y pip

# Heartbeat is written after every successful push; unhealthy when older than 40 min.
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m --retries=3 \
  CMD ["python", "-c", "import os,sys,time; sys.exit(time.time()-os.path.getmtime(os.environ.get('HEARTBEAT_FILE','/tmp/heartbeat'))>2400)"]

# Python is PID 1 (exec form): it handles SIGTERM itself and exits 0. It loops every INTERVAL_SEC (default 900 s).
CMD ["python", "netatmo_to_zo.py"]
