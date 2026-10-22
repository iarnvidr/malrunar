#!/usr/bin/env bash
# obs-control entrypoint.
#   entrypoint.sh            generate the OBS config, start Xvfb, audio, the controller and OBS
#   entrypoint.sh relay      run the UDP/MPEG-TS relay (python3 -m malrunar_obs.relay)
#   entrypoint.sh check      validate the configuration and the plugins, then exit
#   entrypoint.sh <command>  run any other command in the same environment
# Settings: MALRUNAR_OBS_* (docs/obs-control-api.md). Environment of this script only:
#   DISPLAY (:99), MALRUNAR_OBS_XVFB_SCREEN (1280x720x24), XDG_RUNTIME_DIR (/tmp/obs-runtime).
set -euo pipefail

export PYTHONPATH="/opt/malrunar/obs-control/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

case "${1:-obs}" in
  obs) ;;
  relay) exec python3 -m malrunar_obs.relay ;;
  check) exec python3 -m malrunar_obs.setup --check ;;
  *) exec "$@" ;;
esac

export HOME="${HOME:-/config}"
export DISPLAY="${DISPLAY:-:99}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/obs-runtime}"
export QT_X11_NO_MITSHM=1

# OBS reads $XDG_CONFIG_HOME/obs-studio, which is where the generated config is written.
config_dir="$(python3 -m malrunar_obs.setup --print-config-dir)"
export XDG_CONFIG_HOME
XDG_CONFIG_HOME="$(dirname "$config_dir")"

mkdir -p "${XDG_RUNTIME_DIR}" "${XDG_CONFIG_HOME}" /tmp/pulse
chmod 0700 "${XDG_RUNTIME_DIR}"

args_file="$(mktemp)"
python3 -m malrunar_obs.setup --print-args > "${args_file}"
mapfile -d '' obs_args < "${args_file}"
rm -f "${args_file}"

# Chromium stores these process locks beside OBS's persistent browser profile. They name the
# container's hostname and are stale after the container is recreated.
browser_profile="${config_dir}/plugin_config/obs-browser"
mkdir -p "${browser_profile}"
find "${browser_profile}" -maxdepth 1 -type l -name 'Singleton*' -delete

# A container restart can leave Xvfb's lock and socket behind although the old X server is gone.
display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
lock="/tmp/.X${display_number}-lock"
if [[ -f "${lock}" ]]; then
  old_pid="$(tr -d '[:space:]' < "${lock}")"
  if [[ -z "${old_pid}" ]] || ! kill -0 "${old_pid}" 2>/dev/null; then
    unlink "${lock}" || true
    unlink "/tmp/.X11-unix/X${display_number}" || true
  fi
fi

Xvfb "${DISPLAY}" -screen 0 "${MALRUNAR_OBS_XVFB_SCREEN:-1280x720x24}" -nolisten tcp -ac &
for _ in $(seq 1 50); do
  xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1 && break
  sleep 0.1
done

pulseaudio --start --exit-idle-time=-1 --log-target=stderr || true
openbox > /tmp/openbox.log 2>&1 &

# The controller answers /ready with 503 until OBS is up; if it ever exits, start it again.
(
  while true; do
    python3 -m malrunar_obs.controller || true
    sleep 2
  done
) &

exec obs "${obs_args[@]}"
