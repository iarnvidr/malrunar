#!/usr/bin/env bash
# Smoke test for the obs-control image: scripts/smoke.sh obs-control [image-ref]
# Runs the unit tests inside the image, then starts the image with a sample config and checks that
# OBS starts under Xvfb, the generated scenes exist, the API answers, the program preview is a
# JPEG and the output stream carries H.264 and AAC.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "obs-control smoke failed: $*" >&2; exit 1; }
pin() { grep -h "^$1=" "$here/../../obs-runtime/pins.env" | head -1 | cut -d= -f2-; }
obs_version="$(pin OBS_UPSTREAM_VERSION)"
[ -n "$obs_version" ] || fail 'OBS_UPSTREAM_VERSION pin missing'
# Every container run is bounded so a hung GUI process can never stall CI.
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$image" "$@"; }
name="obs-control-smoke-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
exec_in() { timeout -k 10 120 docker exec "$name" "$@"; }
api() { exec_in curl -fsS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }
# Like api, but also returns the body of an error status (/health answers 503 while the output is off).
api_any() { exec_in curl -sS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }

echo '== unit tests inside the image'
if ! output="$(timeout -k 10 300 docker run --rm --entrypoint '' -v "$here:/test:ro" \
    -e PYTHONPATH=/opt/malrunar/obs-control/lib -e PYTHONDONTWRITEBYTECODE=1 "$image" \
    python3 -m unittest discover -s /test -v 2>&1)"; then
    printf '%s\n' "$output" | tail -n 60 >&2
    fail 'unit tests'
fi
printf '%s\n' "$output" | grep -E 'skipped|^Ran |^OK' || true

echo '== image layout: runs as the obs user, no product names, no site configuration'
run bash -c '
    set -e
    test "$(id -u):$(id -g)" = "1000:1000"
    test -x /opt/malrunar/obs-control/entrypoint.sh
    test -s /usr/local/share/malrunar/obs-control/examples/obs-control.example.toml
    if grep -rIiE "g[i]nnungagap|rod[e]kors|r[e]d cross|[e]stho|[e]sben|\\b[a]lice\\b|t[a]ilscale|[a]uthelia|c[a]ll studio|v[d]o\\.[n]inja" \
        /opt/malrunar/obs-control /usr/local/share/malrunar/obs-control; then echo "product-specific text in the image" >&2; exit 1; fi
' || fail 'image layout'
run bash -c 'python3 -c "import aiohttp, websockets; print(aiohttp.__version__, websockets.__version__)"' || fail 'python dependencies'

echo '== configuration errors are reported and stop the start'
if output="$(timeout -k 10 120 docker run --rm "$image" check 2>&1)"; then fail 'a missing camera URL was accepted'; fi
printf '%s\n' "$output" | grep -q 'browser-source URL' || { printf '%s\n' "$output" >&2; fail 'unexpected configuration error text'; }
timeout -k 10 120 docker run --rm -v "$here/smoke-config.toml:/etc/malrunar/obs-control.toml:ro" "$image" check || fail 'sample config does not validate'

echo '== OBS starts under Xvfb with the sample config'
timeout -k 10 120 docker run -d --name "$name" -v "$here:/test:ro" \
    -v "$here/smoke-config.toml:/etc/malrunar/obs-control.toml:ro" "$image" >/dev/null || fail 'docker run'
# Receive the output stream before the output starts (UDP to a closed port would make FFmpeg stop).
exec_in bash -c 'nohup python3 /test/udp-capture.py /tmp/out.ts 18096 240 >/tmp/capture.log 2>&1 &' || fail 'capture start'
ready=false
for _ in $(seq 1 90); do
    if body="$(api /ready 2>/dev/null)" && printf '%s' "$body" | grep -q '"ready": true'; then ready=true; break; fi
    docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q true || break
    sleep 2
done
if [ "$ready" != true ]; then
    docker logs "$name" 2>&1 | tail -n 60 >&2 || true
    fail 'the API did not become ready'
fi
printf '%s' "$body" | grep -qF "\"obsVersion\": \"$obs_version\"" || { echo "$body" >&2; fail "OBS does not report version $obs_version"; }
if docker logs "$name" 2>&1 | grep -E "Failed to load.*(obs-browser|obs-ffmpeg|obs-x264)|Module '.*(obs-browser|obs-ffmpeg|obs-x264).*' not loaded"; then fail 'a core plugin failed to load'; fi

echo '== the generated scenes exist in OBS'
scenes="$(api /api/scenes)"
for scene in 'Smoke Live' 'Smoke Filler' 'Smoke Stopped'; do
    printf '%s' "$scenes" | grep -qF "\"$scene\"" || { echo "$scenes" >&2; fail "scene $scene missing"; }
done
printf '%s' "$scenes" | grep -qF '"program": "Smoke Live"' || { echo "$scenes" >&2; fail 'the program scene is not the first camera'; }
exec_in test -s /config/.config/obs-studio/basic/scenes/smoke.json || fail 'scene collection file missing'

echo '== the output starts and reports healthy'
healthy=false
for _ in $(seq 1 45); do
    if api_any /health 2>/dev/null | grep -q '"healthy": true'; then healthy=true; break; fi
    sleep 2
done
[ "$healthy" = true ] || { docker logs "$name" 2>&1 | tail -n 40 >&2; fail '/health never became healthy'; }

echo "== b-roll: today's video makes the media scene available"
api /api/scene/filler -X POST >/dev/null 2>&1 && fail 'the media scene was selectable without b-roll'
exec_in bash -c '
    set -e
    day="$(date +%F)"
    mkdir -p /config/recordings/daily-fallback
    ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc=size=640x360:rate=24 -f lavfi -i sine=frequency=500 \
        -t 4 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -shortest "/config/recordings/daily-fallback/$day.mp4"
' || fail 'could not create a b-roll file'
api /api/daily-fallback/status | grep -q '"ready": true' || fail 'b-roll status does not report the file as ready'
api /media/3-minute-video.mp4 -o /dev/null || fail 'b-roll media route'

echo '== scene switching and the program preview'
api /api/scene/filler -X POST | grep -q '"ok": true' || fail 'switching to the media scene'
api /health | grep -qF '"scene": "Smoke Filler"' || fail 'the program scene did not change'
api /api/scene/live -X POST | grep -q '"ok": true' || fail 'switching to the camera scene'
sleep 3
exec_in bash -c 'curl -fsS --max-time 30 -D /tmp/preview.headers -o /tmp/preview.jpg http://127.0.0.1:18095/api/program-preview.jpg' || fail 'program preview request'
exec_in bash -c '
    set -e
    test "$(stat -c %s /tmp/preview.jpg)" -gt 500
    test "$(head -c 2 /tmp/preview.jpg | od -An -tx1 | tr -d " \n")" = ffd8
    grep -qi "^content-type: image/jpeg" /tmp/preview.headers
' || fail 'the program preview is not a non-empty JPEG'
exec_in bash -c 'timeout 10 curl -sS --max-time 8 http://127.0.0.1:18095/api/program-live.mjpg 2>/dev/null | head -c 3000 | grep -aq -- "--frame"' || fail 'the MJPEG stream delivered no frame'
api / | grep -q 'OBS control' || fail 'console page'

echo '== the output stream carries H.264 video and AAC audio'
for _ in $(seq 1 40); do
    exec_in test -s /tmp/out.ts.done 2>/dev/null && break
    sleep 3
done
exec_in test -s /tmp/out.ts.done || { exec_in cat /tmp/capture.log >&2 || true; fail 'the capture did not finish'; }
streams="$(exec_in ffprobe -v error -show_entries stream=codec_name,codec_type -of csv=p=0 /tmp/out.ts)"
printf '%s\n' "$streams" | grep -q '^h264,video' || { echo "$streams" >&2; fail 'no H.264 video in the output'; }
printf '%s\n' "$streams" | grep -q '^aac,audio' || { echo "$streams" >&2; fail 'no AAC audio in the output'; }

echo '== output control and clean shutdown'
api /api/stop-output -X POST | grep -q '"ok": true' || fail 'stop-output'
api_any /health 2>/dev/null | grep -q '"healthy": false' || fail 'output still active after stop-output'
api /api/start-output -X POST | grep -q '"ok": true' || fail 'start-output'
echo 'obs-control smoke passed'
