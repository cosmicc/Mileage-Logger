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

prepare_log_paths() {
  local log_dir="${LOG_DIR:-/data/logs}"
  local login_failure_log_path="${LOGIN_FAILURE_LOG_PATH:-/var/log/mileage-logger-login-failures.log}"

  mkdir -p "${log_dir}" "$(dirname "${login_failure_log_path}")"
  if [[ -d "${login_failure_log_path}" ]]; then
    echo "LOGIN_FAILURE_LOG_PATH points to a directory, expected a writable log file: ${login_failure_log_path}" >&2
    exit 1
  fi
  touch "${login_failure_log_path}"
  chown -R app:app "${log_dir}"
  chown app:app "${login_failure_log_path}"
  chmod 0750 "${log_dir}"
  chmod 0640 "${login_failure_log_path}"
}

run_as_app() {
  if [[ "$(id -u)" == "0" ]]; then
    exec gosu app "$@"
  fi
  exec "$@"
}

if [[ "$(id -u)" == "0" ]]; then
  prepare_log_paths
fi

if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
  wait_for_database
  if [[ "$(id -u)" == "0" ]]; then
    gosu app alembic upgrade head
  else
    alembic upgrade head
  fi
fi

run_as_app "$@"
