FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY mileage_logger ./mileage_logger
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint
COPY scripts/gas-snapshot-loop.sh /usr/local/bin/gas-snapshot-loop

RUN pip install . \
    && chmod +x /usr/local/bin/docker-entrypoint /usr/local/bin/gas-snapshot-loop \
    && useradd --system --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/logs \
    && chown -R app:app /app /data

EXPOSE 8000

USER app

ENTRYPOINT ["docker-entrypoint"]
CMD ["uvicorn", "mileage_logger.app:app", "--host", "0.0.0.0", "--port", "8000"]
