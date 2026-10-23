#!/usr/bin/env bash
# PipeWire, WirePlumber and pipewire-pulse for the desktop user, run by supervisord under
# dbus-run-session. Stops all three when one of them ends, so supervisord restarts the lot.
set -euo pipefail
: "${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR must be set}"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT TERM INT

pipewire & pids+=($!)
for _ in $(seq 1 50); do
    [[ -S "$XDG_RUNTIME_DIR/pipewire-0" ]] && break
    sleep 0.2
done
wireplumber & pids+=($!)
pipewire-pulse & pids+=($!)
wait -n
echo "audio-session: a PipeWire process ended; stopping the others" >&2
exit 1
