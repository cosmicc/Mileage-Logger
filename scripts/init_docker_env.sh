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

host_data_dir="$(get_env_value HOST_DATA_DIR)"
host_backup_dir="$(get_env_value HOST_BACKUP_DIR)"

if [[ -n "${host_data_dir}" ]]; then
  if mkdir -p "${host_data_dir}" 2>/dev/null; then
    echo "Prepared host app data directory: ${host_data_dir}"
  else
    echo "Could not create host app data directory: ${host_data_dir}" >&2
    echo "Create it before starting Docker, for example:" >&2
    echo "  sudo install -d -m 0750 ${host_data_dir}" >&2
  fi
fi

if [[ -n "${host_backup_dir}" ]]; then
  if mkdir -p "${host_backup_dir}" 2>/dev/null; then
    echo "Prepared host automatic backup directory: ${host_backup_dir}"
  else
    echo "Could not create host automatic backup directory: ${host_backup_dir}" >&2
    echo "Create it before starting Docker, for example:" >&2
    echo "  sudo install -d -m 0750 ${host_backup_dir}" >&2
  fi
fi

echo "Created ${target}. Review it, then run: docker compose up -d --build"
