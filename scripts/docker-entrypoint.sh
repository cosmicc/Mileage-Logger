#!/usr/bin/env bash
set -Eeuo pipefail

wait_for_database() {
  local timeout="${DB_WAIT_TIMEOUT_SECONDS:-60}"
  python - "$timeout" <<'PY'
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError

from mileage_logger.config import Settings
from mileage_logger.database_engine import database_engine_options, normalized_database_url

timeout = int(sys.argv[1])
settings = Settings()
deadline = time.time() + timeout
last_error = None

while time.time() < deadline:
    try:
        engine = create_engine(
            normalized_database_url(settings.database_url),
            **database_engine_options(settings),
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        raise SystemExit(0)
    except (ArgumentError, ModuleNotFoundError) as exc:
        raise SystemExit(f"Database URL is invalid: {exc}") from exc
    except Exception as exc:
        last_error = exc
        time.sleep(2)

raise SystemExit(f"Database did not become ready within {timeout}s: {last_error}")
PY
}

prepare_runtime_paths() {
  local app_data_dir="${APP_DATA_DIR:-/data}"
  local automatic_backup_dir="${AUTOMATIC_BACKUP_DIR:-${app_data_dir%/}/backups}"
  local owntracks_buffer_path="${OWNTRACKS_BUFFER_PATH:-/data/owntracks-buffer/owntracks-buffer.sqlite3}"
  local owntracks_buffer_fallback_path="${OWNTRACKS_BUFFER_FALLBACK_PATH:-/data/owntracks-buffer-fallback/owntracks-buffer.sqlite3}"

  mkdir -p \
    "${app_data_dir}" \
    "${automatic_backup_dir}" \
    "$(dirname "${owntracks_buffer_path}")" \
    "$(dirname "${owntracks_buffer_fallback_path}")"
  chown -R app:app "${app_data_dir}"
  chown -R app:app "$(dirname "${owntracks_buffer_path}")"
  chown -R app:app "$(dirname "${owntracks_buffer_fallback_path}")"
  chmod 0750 "${app_data_dir}"
  chmod 0750 "${automatic_backup_dir}"
  chmod 0750 "$(dirname "${owntracks_buffer_path}")"
  chmod 0750 "$(dirname "${owntracks_buffer_fallback_path}")"
}

run_as_app() {
  if [[ "$(id -u)" == "0" ]]; then
    exec gosu app "$@"
  fi
  exec "$@"
}

if [[ "$(id -u)" == "0" ]]; then
  prepare_runtime_paths
fi

if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
  if wait_for_database; then
    if [[ "$(id -u)" == "0" ]]; then
      gosu app alembic upgrade head
    else
      alembic upgrade head
    fi
  elif [[ "${OWNTRACKS_BUFFER_ENABLED:-true}" == "true" ]]; then
    echo "Database is unavailable; starting app in OwnTracks buffer limp mode." >&2
  else
    echo "Database is unavailable and OWNTRACKS_BUFFER_ENABLED is not true." >&2
    exit 1
  fi
fi

run_as_app "$@"
