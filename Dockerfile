FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snipalert/ ./snipalert/

VOLUME ["/data"]

CMD ["python", "-m", "snipalert.run"]
