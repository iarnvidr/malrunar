#!/usr/bin/env bash
# Mode-aware liveness check: relay's control API exists without a controller /ready endpoint.
set -euo pipefail
port="${MALRUNAR_OBS_CONTROL_PORT:-18095}"
if [[ "${1:-${MALRUNAR_OBS_MODE:-controller}}" == relay ]] ||
   tr '\0' '\n' < /proc/1/cmdline | grep -qxE 'relay|malrunar_obs\.relay'; then
  port="${MALRUNAR_OBS_RELAY_CONTROL_PORT:-18097}"
  host="${MALRUNAR_OBS_RELAY_CONTROL_HOST:-${MALRUNAR_OBS_RELAY_BIND_HOST:-127.0.0.1}}"
  case "$host" in 0.0.0.0) host=127.0.0.1 ;; ::) host='[::1]' ;; esac
  exec curl -fsS --max-time 4 "http://${host}:${port}/health" >/dev/null
fi
exec curl -fsS --max-time 4 "http://127.0.0.1:${port}/ready" | grep -q '"ready": true'
