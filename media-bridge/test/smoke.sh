#!/usr/bin/env bash
# Smoke test for the media-bridge component: scripts/smoke.sh media-bridge [image-ref]
# The component is FROM scratch, so it is checked from its files and then exercised the way a
# consumer uses it: copied into a desktop built on desktop-base (test/consumer/Dockerfile), where
# a generated MPEG-TS stream is sent to the bridge.
#
# Needs a desktop-base image: $DESKTOP_BASE_REF, else the local tag scripts/build.sh created.
# Optional: MALRUNAR_SMOKE_V4L2_DEVICE=/dev/videoN runs the same test against a v4l2loopback
# device on this host (video sink v4l2) in addition to the file sink.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "media-bridge smoke failed: $*" >&2; exit 1; }
desktop_base="${DESKTOP_BASE_REF:-malrunar/desktop-base:h-$(python3 "$root/scripts/malrunar_image.py" hash desktop-base)}"
docker image inspect "$desktop_base" >/dev/null 2>&1 || fail "desktop-base image $desktop_base is not available locally (build it with scripts/build.sh desktop-base --load, or set DESKTOP_BASE_REF)"

work="$(mktemp -d)"
cid=""
cleanup() { [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

echo '== files'
cid="$(docker create "$image" /unused)"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/media-bridge/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
for f in bin/malrunar-media-bridge bin/malrunar-bridge-logfilter.sh bin/malrunar-xrdp-audio-tune bin/malrunar-media-profile host/setup-v4l2loopback.sh; do
    test -x "$work/out/$f" || fail "$f is missing or not executable"
done
for f in README.md LICENSE share/supervisord-media-bridge.conf.example share/malrunar-media-bridge.service.example; do
    test -s "$work/out/$f" || fail "$f is missing"
done
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" \
    || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-media-bridge"
bash -n "$work/out/bin/malrunar-bridge-logfilter.sh"
bash -n "$work/out/host/setup-v4l2loopback.sh"
sh -n "$work/out/bin/malrunar-xrdp-audio-tune"
python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$work/out/bin/malrunar-media-profile"

echo '== bridge script against stub ffmpeg/pactl: sinks, bounded file, config checks, startup log filter'
"$here/test-bridge-offline.sh" "$work/out/bin"

echo '== host setup script (stubbed kernel module)'
"$here/test-host-script.sh" "$work/out/host/setup-v4l2loopback.sh"

echo '== consumer image: desktop-base + the component'
consumer="malrunar-test/media-bridge-consumer:$$"
docker build --quiet --tag "$consumer" \
    --build-arg "BRIDGE_IMAGE=$image" --build-arg "DESKTOP_BASE=$desktop_base" \
    "$here/consumer" >/dev/null
trap 'docker rmi -f "$consumer" >/dev/null 2>&1 || true; cleanup' EXIT
run() { timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 "$@"; }

echo '== media-profile helper'
run "$consumer" bash -c '
    set -e
    p=/opt/malrunar/media-bridge/bin/malrunar-media-profile
    $p https://meet.example/ "https://[*.]example.org/" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d[\"VideoCaptureAllowed\"] is False and len(d[\"VideoCaptureAllowedUrls\"])==2"
    $p --browser firefox https://meet.example | python3 -c "import json,sys; d=json.load(sys.stdin); assert d[\"policies\"][\"Permissions\"][\"Camera\"][\"Allow\"]==[\"https://meet.example\"]"
    if $p http://insecure.example >/dev/null 2>&1; then exit 1; fi
' || fail 'media-profile helper'

echo '== bridge with the bounded file video sink (the documented fallback)'
run "$consumer" /usr/local/bin/bridge-test.sh file || fail 'file-sink bridge test'

echo '== bridge with the null video sink (decode and discard)'
run "$consumer" /usr/local/bin/bridge-test.sh null || fail 'null-sink bridge test'

if [ -n "${MALRUNAR_SMOKE_V4L2_DEVICE:-}" ]; then
    dev="$MALRUNAR_SMOKE_V4L2_DEVICE"
    echo "== bridge with the v4l2loopback device $dev"
    [ -c "$dev" ] || fail "$dev is not a character device on this host"
    timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 \
        --device "$dev" --group-add "$(stat -c %g "$dev")" \
        "$consumer" /usr/local/bin/bridge-test.sh v4l2 "$dev" || fail 'v4l2 bridge test'
fi
echo 'media-bridge smoke passed'
