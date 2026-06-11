#!/usr/bin/env bash
set -Eeuo pipefail

wait_for_database() {
  local timeout="${DB_WAIT_TIMEOUT_SECONDS:-60}"
  python - "$timeout" <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

timeout = int(sys.argv[1])
database_url = os.environ["DATABASE_URL"]
deadline = time.time() + timeout
last_error = None

while time.time() < deadline:
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(2)

raise SystemExit(f"Database did not become ready within {timeout}s: {last_error}")
PY
}

if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
  wait_for_database
  alembic upgrade head
fi

exec "$@"
