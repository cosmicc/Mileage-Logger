#!/usr/bin/env bash
set -Eeuo pipefail

interval="${GAS_SNAPSHOT_INTERVAL_SECONDS:-86400}"

if [[ "${GAS_SNAPSHOT_RUN_ON_STARTUP:-true}" == "true" ]]; then
  mileage-logger gas-snapshot || true
fi

while true; do
  sleep "${interval}"
  mileage-logger gas-snapshot || true
done
