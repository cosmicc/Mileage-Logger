#!/usr/bin/env bash
set -Eeuo pipefail

target="${1:-.env}"
template=".env.docker.example"

if [[ -f "${target}" ]]; then
  echo "${target} already exists. Refusing to overwrite it." >&2
  exit 1
fi

if [[ ! -f "${template}" ]]; then
  echo "Missing ${template}" >&2
  exit 1
fi

get_env_value() {
  local key="$1"
  python3 - "${target}" "${key}" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1])
key = sys.argv[2]

for line in target.read_text().splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        continue
    name, value = stripped.split("=", 1)
    if name == key:
        print(value.strip().strip('"').strip("'"))
        break
PY
}

python3 - "${template}" "${target}" <<'PY'
from pathlib import Path
import secrets
import sys

template = Path(sys.argv[1])
target = Path(sys.argv[2])

replacements = {
    "change-me": secrets.token_hex(32),
    "change-web-login-password": secrets.token_urlsafe(32),
    "change-web-api-key": secrets.token_urlsafe(32),
    "change-postgres-password": secrets.token_urlsafe(32),
    "change-owntracks-password": secrets.token_urlsafe(32),
    "change-owntracks-encryption-key": secrets.token_urlsafe(24),
}

content = template.read_text()
for old, new in replacements.items():
    content = content.replace(old, new)

target.write_text(content)
PY

chmod 0600 "${target}"

host_log_dir="$(get_env_value HOST_LOG_DIR)"
host_login_failure_log_path="$(get_env_value HOST_LOGIN_FAILURE_LOG_PATH)"
login_failure_log_filename="mileage-logger-login-failures.log"

if [[ -n "${host_log_dir}" ]]; then
  if mkdir -p "${host_log_dir}" 2>/dev/null; then
    echo "Prepared host app log directory: ${host_log_dir}"
    if mkdir -p "${host_log_dir%/}/backups" 2>/dev/null; then
      echo "Prepared host automatic backup directory: ${host_log_dir%/}/backups"
    fi
  else
    echo "Could not create host app log directory: ${host_log_dir}" >&2
    echo "Create it before starting Docker, for example:" >&2
    echo "  sudo install -d -m 0750 ${host_log_dir}" >&2
  fi
fi

if [[ -n "${host_log_dir}" ]]; then
  login_failure_log_path="${host_log_dir%/}/${login_failure_log_filename}"
  if touch "${login_failure_log_path}" 2>/dev/null; then
    echo "Prepared host login failure log file: ${login_failure_log_path}"
  else
    echo "Could not create host login failure log file: ${login_failure_log_path}" >&2
    echo "Create it before starting Docker, for example:" >&2
    echo "  sudo install -m 0640 /dev/null ${login_failure_log_path}" >&2
  fi
fi

if [[ -n "${host_login_failure_log_path}" && -n "${host_log_dir}" ]]; then
  login_failure_log_path="${host_log_dir%/}/${login_failure_log_filename}"
  if [[ -d "${host_login_failure_log_path}" ]]; then
    echo "Host login failure log alias is a directory, expected a file or symlink: ${host_login_failure_log_path}" >&2
    echo "If it is empty, remove it and create the symlink with:" >&2
    echo "  sudo rmdir ${host_login_failure_log_path}" >&2
    echo "  sudo ln -sfn ${login_failure_log_path} ${host_login_failure_log_path}" >&2
  elif [[ -e "${host_login_failure_log_path}" && ! -L "${host_login_failure_log_path}" ]]; then
    echo "Host login failure log alias already exists as a regular file: ${host_login_failure_log_path}" >&2
    echo "The app writes to ${login_failure_log_path}; replace the alias manually if you want the short /var/log path." >&2
  elif ln -sfn "${login_failure_log_path}" "${host_login_failure_log_path}" 2>/dev/null; then
    echo "Prepared host login failure log alias: ${host_login_failure_log_path} -> ${login_failure_log_path}"
  else
    echo "Could not create host login failure log alias: ${host_login_failure_log_path}" >&2
    echo "Create it before starting Docker, for example:" >&2
    echo "  sudo ln -sfn ${login_failure_log_path} ${host_login_failure_log_path}" >&2
  fi
fi

echo "Created ${target}. Review it, then run: docker compose up -d --build"
