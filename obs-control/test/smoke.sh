#!/usr/bin/env bash
# Smoke test for the obs-control image: scripts/smoke.sh obs-control [image-ref]
# Runs the unit tests inside the image, then starts the image with a sample config and checks that
# OBS starts under Xvfb, the generated scenes exist, the API answers, the program preview is a
# JPEG and the output stream carries H.264 and AAC. The sample config also enables the takes plugin:
# upload slides (PNG and a PDF), arm, record a short take from the test card, assemble it with a slide
# and play the result into the program.
#
# Remote-daemon safe: nothing of this machine is bind-mounted. The test files and the sample config
# reach the containers with `docker cp` between `docker create` and `docker start`.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "obs-control smoke failed: $*" >&2; exit 1; }
# Self-check: no host bind mounts (bracket expressions keep this check from matching itself).
if grep -nE '[-]v[ =]+"?(\$here|\$\{here|\$PWD|\$work|\$\(|/|\./|~)|[-][-]mount|[-][-]volume|type=[b]ind' "${BASH_SOURCE[0]}"; then
    fail 'the script bind-mounts a host path (not remote-daemon safe)'
fi
pin() { grep -h "^$1=" "$here/../../obs-runtime/pins.env" | head -1 | cut -d= -f2-; }
obs_version="$(pin OBS_UPSTREAM_VERSION)"
[ -n "$obs_version" ] || fail 'OBS_UPSTREAM_VERSION pin missing'
# Every container run is bounded so a hung GUI process can never stall CI.
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$image" "$@"; }
name="obs-control-smoke-$$"
created=()   # containers made with docker create for one-off runs
cleanup() { docker rm -f "$name" ${created[@]+"${created[@]}"} >/dev/null 2>&1 || true; }
trap cleanup EXIT
# put_dir <container> <host dir> <absolute dir in the container>: copy a directory into a created,
# not yet started container (root-owned, world-readable, whatever the host's umask).
put_dir() {
    tar -C "$2" --owner=0 --group=0 --mode='u+rwX,go+rX' --transform "s,^\./*,${3#/}/," -cf - . \
        | docker cp - "$1:/"
}
# put_config <container>: the sample config as /etc/malrunar/obs-control.toml (the default config path).
put_config() {
    local stage
    stage="$(mktemp -d)"
    mkdir -p "$stage/etc/malrunar"
    cp "$here/smoke-config.toml" "$stage/etc/malrunar/obs-control.toml"
    tar -C "$stage" --owner=0 --group=0 --mode='u+rwX,go+rX' -cf - etc | docker cp - "$1:/"
    rm -rf "$stage"
}
# create_run <docker create args...>: create a one-off container, copy the test files in (and the
# sample config when WITH_CONFIG=1), run it to completion in the foreground (attached, bounded) and
# remove it.
create_run() {
    local cid status=0
    cid="$(docker create "$@")" || return 1
    created+=("$cid")
    put_dir "$cid" "$here" /test || return 1
    if [ "${WITH_CONFIG:-0}" = 1 ]; then put_config "$cid" || return 1; fi
    timeout -k 10 300 docker start -a "$cid" || status=$?
    docker rm -f "$cid" >/dev/null 2>&1 || true
    return "$status"
}
exec_in() { timeout -k 10 120 docker exec "$name" "$@"; }
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
api() { exec_in curl -fsS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }
# Like api, but also returns the body of an error status (/health answers 503 while the output is off).
api_any() { exec_in curl -sS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }

echo '== unit tests inside the image'
if ! output="$(create_run --entrypoint '' \
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
    if grep -rIiE "g[i]nnungagap|rod[e]kors|r[e]d cross|[e]stho|[e]sben|\\b[a]lice\\b|t[a]ilscale|c[a]ll studio" \
        /opt/malrunar/obs-control /usr/local/share/malrunar/obs-control; then echo "product-specific text in the image" >&2; exit 1; fi
' || fail 'image layout'
run bash -c 'python3 -c "import aiohttp, websockets; print(aiohttp.__version__, websockets.__version__)"' || fail 'python dependencies'

echo '== configuration errors are reported and stop the start'
if output="$(timeout -k 10 120 docker run --rm "$image" check 2>&1)"; then fail 'a missing camera URL was accepted'; fi
printf '%s\n' "$output" | grep -q 'browser-source URL' || { printf '%s\n' "$output" >&2; fail 'unexpected configuration error text'; }
WITH_CONFIG=1 create_run "$image" check || fail 'sample config does not validate'

echo '== OBS starts under Xvfb with the sample config'
timeout -k 10 120 docker create --name "$name" "$image" >/dev/null || fail 'docker create'
put_dir "$name" "$here" /test && put_config "$name" || fail 'copying the test files into the container'
timeout -k 10 120 docker start "$name" >/dev/null || fail 'docker start'
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

echo '== takes: slides (PNG and PDF), arm, record a take from the test card, assemble, play into the program'
# The output goes to a closed UDP port once the capture above is done; keep a receiver on it.
exec_in bash -c 'nohup python3 /test/udp-drain.py 18096 >/dev/null 2>&1 &' || fail 'drain start'
api /api/takes/status | grep -q '"armed": false' || fail 'takes status (plugin not loaded?)'
exec_in bash -c '
    set -e
    ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i color=c=0x2060a0:size=800x450 -frames:v 1 /tmp/slide.png
    python3 /test/make-pdf.py 2 /tmp/slides.pdf
    echo "not an image" > /tmp/notes.txt
' || fail 'could not create slide files'
upload() { exec_in curl -sS --max-time 60 -w '\n%{http_code}' -F "name=$2" -F "file=@$1" http://127.0.0.1:18095/api/takes/slides; }
png="$(upload /tmp/slide.png Picture)"
[ "$(printf '%s' "$png" | tail -n 1)" = 201 ] || { echo "$png" >&2; fail 'PNG slide upload'; }
deck="$(printf '%s' "$png" | head -n -1 | json 'd["slides"]["id"]')" || fail 'PNG upload response'
pdf="$(upload /tmp/slides.pdf Pages)"
[ "$(printf '%s' "$pdf" | tail -n 1)" = 201 ] || { echo "$pdf" >&2; fail 'PDF slide upload (pdftoppm)'; }
[ "$(printf '%s' "$pdf" | head -n -1 | json 'd["slides"]["pages"]')" = 2 ] || { echo "$pdf" >&2; fail 'the PDF did not become two slides'; }
text="$(upload /tmp/notes.txt Notes)"
[ "$(printf '%s' "$text" | tail -n 1)" = 415 ] || { echo "$text" >&2; fail 'a text file was accepted as a slide'; }
api "/api/takes/slides/$deck/1" -o /tmp/slide-back.png || fail 'slide picture route'
exec_in bash -c 'head -c 4 /tmp/slide-back.png | od -An -tx1 | tr -d " \n" | grep -q "^89504e47"' || fail 'the slide picture is not a PNG'

api /api/takes/arm -X POST -H 'Content-Type: application/json' \
    -d "{\"layout\": \"pip\", \"slide\": {\"deck_id\": \"$deck\", \"page\": 1}}" | grep -q '"armed": true' || fail 'arm'
healthy=false
for _ in $(seq 1 30); do
    if api_any /health 2>/dev/null | grep -qF '"scene": "Take Studio"'; then healthy=true; break; fi
    sleep 1
done
[ "$healthy" = true ] || fail 'the take scene is not on the program'
healthy=false
for _ in $(seq 1 30); do
    if api_any /health 2>/dev/null | grep -q '"healthy": true'; then healthy=true; break; fi
    sleep 2
done
[ "$healthy" = true ] || { docker logs "$name" 2>&1 | tail -n 30 >&2; fail 'the program output did not come back after the canvas change'; }
api /api/takes/layout -X POST -H 'Content-Type: application/json' -d '{"layout": "camera"}' | grep -q '"layout": "camera"' || fail 'layout'
started=false
for _ in $(seq 1 30); do   # the camera page needs a moment before its picture is not black
    if api_any /api/takes/start -X POST -H 'Content-Type: application/json' -d '{"take_id": "smoke-take"}' | grep -q '"take_id": "smoke-take"'; then started=true; break; fi
    sleep 2
done
[ "$started" = true ] || { docker logs "$name" 2>&1 | tail -n 30 >&2; fail 'the take did not start'; }
sleep 7
take="$(api /api/takes/stop -X POST)" || fail 'stop take'
printf '%s' "$take" | grep -q '"has_audio": true' || { echo "$take" >&2; fail 'the take has no audio stream'; }
take_file=/config/recordings/takes/shared/takes/smoke-take.mp4
streams="$(exec_in ffprobe -v error -show_entries stream=codec_name,codec_type,width,height -of csv=p=0 "$take_file")" || fail 'probe take'
printf '%s\n' "$streams" | grep -q '^h264,video,1920,1080\|^h264,1920,1080,video' || { echo "$streams" >&2; fail 'the take is not 1080p H.264'; }
printf '%s\n' "$streams" | grep -q 'aac' || { echo "$streams" >&2; fail 'the take has no AAC audio'; }
api /api/takes/library | grep -qF '"take_id": "smoke-take"' || fail 'the take is not in the library'

job="$(api /api/takes/assemble -X POST -H 'Content-Type: application/json' -d "{\"name\": \"Smoke video\", \"items\": [
    {\"type\": \"take\", \"take_id\": \"smoke-take\", \"start\": 1, \"end\": 4},
    {\"type\": \"still\", \"deck_id\": \"$deck\", \"page\": 1, \"seconds\": 2}]}")" || fail 'assemble'
job_id="$(printf '%s' "$job" | json 'd["job"]["job_id"]')"
state=running
for _ in $(seq 1 60); do
    result="$(api "/api/takes/jobs/$job_id")"
    state="$(printf '%s' "$result" | json 'd["job"]["state"]')"
    [ "$state" = running ] || break
    sleep 2
done
[ "$state" = "done" ] || { echo "$result" >&2; fail 'the assembly did not finish'; }
clip_id="$(printf '%s' "$result" | json 'd["job"]["clip"]["id"]')"
clip_scene="$(printf '%s' "$result" | json 'd["job"]["clip"]["scene_name"]')"
clip_streams="$(exec_in ffprobe -v error -show_entries stream=codec_name,codec_type,width,height -of csv=p=0 "/config/recordings/$clip_id.mp4")"
printf '%s\n' "$clip_streams" | grep -q 'h264' || { echo "$clip_streams" >&2; fail 'the assembled clip has no H.264 video'; }
printf '%s\n' "$clip_streams" | grep -q 'aac' || { echo "$clip_streams" >&2; fail 'the assembled clip has no AAC audio'; }
api /api/clips | grep -qF "\"$clip_id\"" || fail 'the assembled clip is not in the clip library'

api /api/takes/disarm -X POST | grep -q '"armed": false' || fail 'disarm'
api /api/scene/live -X POST >/dev/null || fail 'back to the camera scene'
healthy=false
for _ in $(seq 1 30); do
    if api_any /health 2>/dev/null | grep -q '"healthy": true'; then healthy=true; break; fi
    sleep 2
done
[ "$healthy" = true ] || fail 'the program output did not come back after leaving take mode'
api "/api/clip/$clip_id/scene" -X POST | grep -q '"ok": true' || fail 'playing the assembled clip into the program'
[ "$(api_any /health | json 'd["scene"]')" = "$clip_scene" ] || fail 'the program scene is not the assembled clip'
sleep 3
exec_in bash -c 'curl -fsS --max-time 30 -o /tmp/take-preview.jpg http://127.0.0.1:18095/api/program-preview.jpg && test "$(stat -c %s /tmp/take-preview.jpg)" -gt 500' || fail 'program preview of the assembled clip'
api /api/takes/status | grep -q '"armed": false' || fail 'take mode is still armed'

echo '== output control and clean shutdown'
api /api/stop-output -X POST | grep -q '"ok": true' || fail 'stop-output'
api_any /health 2>/dev/null | grep -q '"healthy": false' || fail 'output still active after stop-output'
api /api/start-output -X POST | grep -q '"ok": true' || fail 'start-output'
echo 'obs-control smoke passed'

echo '== relay healthcheck detects command mode without an override'
relay_name="${name}-relay"
created+=("$relay_name")
docker create --name "$relay_name" -e MALRUNAR_OBS_RELAY_TARGET_HOST=127.0.0.1 \
    -e MALRUNAR_OBS_RELAY_CONTROL_HOST=0.0.0.0 "$image" relay >/dev/null
docker start "$relay_name" >/dev/null
relay_ready=false
for _ in $(seq 1 30); do
    if docker exec "$relay_name" /opt/malrunar/obs-control/healthcheck.sh; then relay_ready=true; break; fi
    sleep 1
done
[ "$relay_ready" = true ] || fail 'relay healthcheck did not detect the relay process'
