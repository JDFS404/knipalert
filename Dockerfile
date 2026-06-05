FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY knipalert/ ./knipalert/

VOLUME ["/data"]

# Unhealthy if the gateway heartbeat file goes stale (>150s) -> Komodo can alert.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD test "$(( $(date +%s) - $(stat -c %Y /tmp/alive 2>/dev/null || echo 0) ))" -lt 150 || exit 1

CMD ["python", "-m", "knipalert.run"]
