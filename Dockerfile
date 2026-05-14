# ---- builder: install deps into an isolated directory ----
FROM python:3.12-alpine AS builder

WORKDIR /install
RUN pip install --no-cache-dir --target=/install requests==2.32.*

# ---- final: clean Alpine Python, no pip, no cache ----
FROM python:3.12-alpine

WORKDIR /app

COPY --from=builder /install /app/deps
COPY netatmo_to_zo.py .

ENV PYTHONPATH=/app/deps \
    PYTHONUNBUFFERED=1

# Run every 15 minutes (900 s)
CMD ["sh", "-c", "while true; do python netatmo_to_zo.py; sleep 900; done"]
