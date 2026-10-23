#!/usr/bin/env bash
# Runs the quickstart exactly as README.md documents it and checks the result. Used by
# .github/workflows/quickstart.yml; also works on a machine with Docker:
#   examples/quickstart/test/run.sh
# Image references come from the environment (MALRUNAR_DESKTOP_BASE_IMAGE, MALRUNAR_MEDIA_BRIDGE_IMAGE,
# MALRUNAR_OBS_CONTROL_IMAGE); without them the published images are used. Every command is bounded.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

compose_file=examples/quickstart/compose.yaml
api=http://127.0.0.1:18095
ready_attempts="${QUICKSTART_READY_ATTEMPTS:-90}"
compose() { timeout -k 10 "${COMPOSE_TIMEOUT:-120}" docker compose -f "$compose_file" "$@"; }
fail() { echo "QUICKSTART: FAILED: $*" >&2; exit 1; }

cleanup() {
    status=$?
    if [[ "$status" -ne 0 ]]; then
        echo "QUICKSTART: failed with status $status; container state and logs follow" >&2
        compose ps --all >&2 || true
        COMPOSE_TIMEOUT=120 compose logs --no-color --timestamps --tail 300 >&2 || true
    fi
    compose down -v --remove-orphans || true
    echo "QUICKSTART: cleaned up"
}
trap cleanup EXIT

echo "QUICKSTART: docker compose up"
COMPOSE_TIMEOUT=3000 compose up -d --build
echo "QUICKSTART: started"

# 1. /ready answers 200 with "ready": true once OBS is up.
ready=''
for attempt in $(seq 1 "$ready_attempts"); do
    if body="$(timeout -k 5 15 curl -fsS --max-time 10 "$api/ready" 2>&1)" && grep -q '"ready": true' <<<"$body"; then
        ready="$body"
        echo "QUICKSTART: attempt $attempt/$ready_attempts: ready"
        break
    fi
    echo "QUICKSTART: attempt $attempt/$ready_attempts: not ready yet (${body:-no answer})"
    sleep 4
done
[[ -n "$ready" ]] || fail "$api/ready did not report ready"
echo "$ready"
echo "QUICKSTART: ready OK"

# 2. The API lists the scenes and switches the program scene: A (live) -> B (standby) -> A.
program_of() {
    local body
    body="$(timeout -k 5 30 curl -fsS --max-time 20 "$api/api/scenes")" || fail "GET /api/scenes"
    echo "$body" >&2
    sed -n 's/.*"program": *"\([^"]*\)".*/\1/p' <<<"$body"
}
switch_to() {
    local body
    body="$(timeout -k 5 30 curl -fsS --max-time 20 -X POST "$api/api/scene/$1")" || fail "POST /api/scene/$1"
    echo "$body"
    grep -q '"ok": true' <<<"$body" || fail "POST /api/scene/$1 did not answer ok"
}
scene_a="$(program_of)"
[[ "$scene_a" == "Live Feed" ]] || fail "the program scene at start is '$scene_a', expected 'Live Feed'"
switch_to standby
scene_b="$(program_of)"
[[ "$scene_b" == "Standby" && "$scene_b" != "$scene_a" ]] || fail "the program scene did not change to Standby (now '$scene_b')"
switch_to live
scene_c="$(program_of)"
[[ "$scene_c" == "$scene_a" ]] || fail "the program scene did not return to '$scene_a' (now '$scene_c')"
echo "QUICKSTART: scene switched OK ($scene_a -> $scene_b -> $scene_c)"

# 3. The virtual microphone exists for the desktop user (the bridge may need a moment).
sources=''
for attempt in $(seq 1 30); do
    if sources="$(compose exec -T --user desktop desktop pactl list short sources 2>&1)" \
        && grep -q malrunar_virtual_mic <<<"$sources"; then
        break
    fi
    echo "QUICKSTART: microphone attempt $attempt/30: not there yet"
    sleep 2
done
echo "$sources"
grep -q malrunar_virtual_mic <<<"$sources" || fail "malrunar_virtual_mic is not listed by pactl list short sources"
echo "QUICKSTART: virtual mic present OK"

# Informational: whether OBS's output reached the bridge (the bridge starts it through /api/start-output).
timeout -k 5 30 curl -sS --max-time 20 "$api/health" || true
echo
echo "QUICKSTART: all checks passed"
