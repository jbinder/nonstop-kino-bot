FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Vienna \
    DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 bot \
    && mkdir -p /data && chown bot:bot /data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

USER bot
VOLUME ["/data"]
CMD ["python", "-m", "bot.main"]
