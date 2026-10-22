#!/usr/bin/env bash
# Smoke test for guacamole-tunnel: scripts/smoke.sh guacamole-tunnel [image-ref]
# The image is a service on its own: the test image holds nothing but it (no guacd, no Guacamole).
# Runs the unit tests inside the image, checks the configuration errors and the user, and runs the
# real service against a fake Guacamole upstream inside one container. Every container is bounded.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "guacamole-tunnel smoke failed: $*" >&2; exit 1; }
run() { timeout -k 10 300 docker run --rm "$@"; }

echo '== user, healthcheck, dependencies'
test "$(run --entrypoint id "$image" -u)" = 65534 || fail 'the image does not run as uid 65534'
docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$image" | grep -q -- '--healthcheck' \
    || fail 'no HEALTHCHECK'
run --entrypoint sh "$image" -c '
    for program in guacd guacamole-client; do ! command -v $program >/dev/null || { echo "image contains $program"; exit 1; }; done
    test -z "$(ls /opt/malrunar | grep -v "^guacamole-tunnel$" || true)"
' || fail 'the component must stand alone: no guacd, no other component'

echo '== a missing setting is reported by name (exit status 78)'
status=0
out="$(run "$image" 2>&1)" || status=$?
test "$status" = 78 || fail "expected exit status 78 without settings, got $status: $out"
echo "$out" | grep -q MALRUNAR_GUAC_TUNNEL_UPSTREAM || fail "UPSTREAM was not named: $out"
status=0
out="$(run -e MALRUNAR_GUAC_TUNNEL_UPSTREAM=http://upstream.invalid/gw "$image" 2>&1)" || status=$?
test "$status" = 78 || fail "expected exit status 78 without an identity header name, got $status"
echo "$out" | grep -q MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER || fail "IDENTITY_HEADER was not named: $out"

echo '== unit tests inside the image'
run --entrypoint python3 -v "$here:/test:ro" -e MALRUNAR_GUAC_TUNNEL_CODE=/opt/malrunar/guacamole-tunnel \
    "$image" -B -m unittest discover -v -s /test

echo '== the service process joins a second viewer to the first one (fake upstream)'
run --entrypoint python3 -v "$here:/test:ro" "$image" -B /test/e2e_in_container.py

echo 'guacamole-tunnel smoke passed'
