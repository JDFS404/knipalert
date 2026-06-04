FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY knipalert/ ./knipalert/

VOLUME ["/data"]

CMD ["python", "-m", "knipalert.run"]
