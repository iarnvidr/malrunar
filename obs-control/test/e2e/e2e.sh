#!/usr/bin/env bash
# End-to-end test: obs-control -> MPEG-TS over UDP -> media-bridge in a desktop-base container.
#
#   obs-control/test/e2e/e2e.sh <obs-control image> <media-bridge component image> <desktop-base image> [/dev/videoN]
#
# Starts two containers on a private network: a desktop (desktop-base with the media-bridge
# component copied in, built with media-bridge/test/consumer/Dockerfile) running PipeWire and the
# bridge, and obs-control sending its program to the bridge. Then it checks, through the virtual
# microphone and the video sink, that what OBS has on air arrives, that scene switches made through
# the HTTP API change it (camera page, an uploaded clip), that the bridge restarts the output by itself after the stream stops
# (the bridge calls obs-control's /api/start-output), and that a take prepared with the takes plugin (recorded from
# its own camera page with a 660 Hz tone, assembled with a slide) arrives at the virtual microphone when it is played
# into the program, while the program stays silent as the take is recorded.
#
# The video sink is a file; with a v4l2loopback device given as the fourth argument it is that
# device (mapped into the desktop container), so the whole chain including the virtual camera runs.
# Audio is checked in both cases. Every container run is bounded.
set -euo pipefail
obs_image="${1:?usage: e2e.sh <obs-control image> <media-bridge image> <desktop-base image> [/dev/videoN]}"
bridge_image="${2:?media-bridge component image}"
desktop_base="${3:?desktop-base image}"
v4l2_device="${4:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../../.." && pwd)"
id="obs-control-e2e-$$"
consumer="malrunar-test/obs-control-e2e-desktop:$$"
fail() { echo "obs-control e2e failed: $*" >&2; exit 1; }
step() { echo "== $*"; }
[ -d "$root/media-bridge/test/consumer" ] || fail 'media-bridge/test/consumer is missing (the e2e reuses its desktop image recipe)'

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        for name in obs desktop; do
            echo "--- logs: $name" >&2
            docker logs "$id-$name" 2>&1 | tail -n 50 >&2 || true
        done
        echo '--- bridge log' >&2
        docker exec "$id-desktop" tail -n 30 /tmp/e2e/bridge.log >&2 2>/dev/null || true
    fi
    docker rm -f "$id-obs" "$id-desktop" >/dev/null 2>&1 || true
    docker network rm "$id" >/dev/null 2>&1 || true
    docker volume rm "$id-media" >/dev/null 2>&1 || true
    docker rmi -f "$consumer" >/dev/null 2>&1 || true
}
trap cleanup EXIT

field() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
desk() { timeout -k 10 150 docker exec --user 1000:1000 "$id-desktop" /e2e/desktop.sh "$@"; }
api() { timeout -k 10 60 docker exec "$id-obs" curl -fsS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }
api_any() { timeout -k 10 60 docker exec "$id-obs" curl -sS --max-time 20 "http://127.0.0.1:18095$1" "${@:2}"; }

step 'desktop image: desktop-base + the media-bridge component'
docker build --quiet --tag "$consumer" --build-arg "BRIDGE_IMAGE=$bridge_image" \
    --build-arg "DESKTOP_BASE=$desktop_base" "$root/media-bridge/test/consumer" >/dev/null

step 'test media: video with a 1000 Hz tone (the scene "video"); the camera page carries 440 Hz'
docker network create "$id" >/dev/null
docker volume create "$id-media" >/dev/null
timeout -k 10 300 docker run --rm --user 0 --entrypoint '' -v "$id-media:/media" "$obs_image" bash -c '
    ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i "testsrc=size=640x360:rate=24" \
        -f lavfi -i "sine=frequency=1000:sample_rate=48000" -t 30 -c:v libx264 -preset ultrafast \
        -pix_fmt yuv420p -c:a aac -shortest /media/e2e.mp4
    chmod 0644 /media/e2e.mp4'

step 'start the desktop (PipeWire and the bridge)'
device_args=()
video='file'
if [ -n "$v4l2_device" ]; then
    [ -c "$v4l2_device" ] || fail "$v4l2_device is not a character device"
    device_args=(--device "$v4l2_device" --group-add "$(stat -c %g "$v4l2_device")")
    video="$v4l2_device"
fi
timeout -k 10 120 docker run -d --name "$id-desktop" --network "$id" --network-alias desktop \
    --user 1000:1000 --entrypoint '' "${device_args[@]}" -v "$here:/e2e:ro" \
    -e "E2E_VIDEO=$video" -e E2E_START_URL=http://obs:18095/api/start-output \
    "$consumer" dbus-run-session -- /e2e/desktop.sh serve >/dev/null

step 'start obs-control, sending to the bridge'
timeout -k 10 120 docker run -d --name "$id-obs" --network "$id" --network-alias obs --shm-size 512m \
    -v "$id-media:/media:ro" -v "$here:/e2e:ro" \
    -e MALRUNAR_OBS_CONFIG=/e2e/obs-e2e.toml -e MALRUNAR_OBS_FEATURES_TAKES=true \
    -e MALRUNAR_OBS_CAMERA_URL=file:///usr/local/share/malrunar/obs-control/examples/test-card.html \
    -e MALRUNAR_OBS_MEDIA_FILE=/media/e2e.mp4 -e MALRUNAR_OBS_START_SCENE=video \
    -e MALRUNAR_OBS_BROLL_ENABLED=false \
    -e 'MALRUNAR_OBS_OUTPUT_URL=udp://desktop:5004?pkt_size=1316' \
    "$obs_image" >/dev/null
ready=false
for _ in $(seq 1 120); do
    if api /ready 2>/dev/null | grep -q '"ready": true'; then ready=true; break; fi
    docker inspect -f '{{.State.Running}}' "$id-obs" 2>/dev/null | grep -q true || break
    sleep 2
done
[ "$ready" = true ] || fail 'obs-control did not become ready'

step 'the media scene reaches the virtual microphone (1000 Hz) and the video sink'
desk tone 1000 180 || fail 'no 1000 Hz tone at the virtual microphone'
api_any /health | grep -qF '"scene": "B-roll Video"' || fail 'the program scene is not the media scene'
desk frame media || fail 'no video frame from the media scene'

step 'switch to the camera page through the API (440 Hz, different picture)'
api /api/scene/live -X POST | grep -q '"ok": true' || fail 'POST /api/scene/live'
desk tone 440 90 || fail 'no 440 Hz tone after switching to the camera page'
desk frame camera || fail 'no video frame from the camera page'
difference="$(desk diff media camera)"
echo "mean luma difference between the scenes: $difference"
python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > 8 else 1)" "$difference" || fail 'the picture did not change with the scene'

step 'upload the media as a clip and put its scene on air (1000 Hz again)'
upload="$(timeout -k 10 120 docker exec "$id-obs" curl -fsS --max-time 100 -F name='E2E clip' -F file=@/media/e2e.mp4 http://127.0.0.1:18095/api/clips/upload)" \
    || fail 'POST /api/clips/upload'
clip_id="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['clip']['id'])" "$upload")" || fail "unexpected upload response: $upload"
api /api/clips | grep -qF "\"$clip_id\"" || fail 'the uploaded clip is not listed'
api "/api/clip/$clip_id/scene" -X POST | grep -q '"ok": true' || fail 'POST /api/clip/<id>/scene'
desk tone 1000 60 || fail 'no 1000 Hz tone after switching to the clip'

step 'stop the output: the bridge notices, restarts and asks obs-control to start it again'
api /api/stop-output -X POST | grep -q '"ok": true' || fail 'POST /api/stop-output'
api_any /health | grep -q '"healthy": false' || fail 'the output did not stop'
healthy=false
for _ in $(seq 1 60); do
    if api_any /health | grep -q '"healthy": true'; then healthy=true; break; fi
    sleep 2
done
[ "$healthy" = true ] || fail 'the bridge did not restart the output'
desk tone 1000 90 || fail 'no tone after the bridge restarted the output'

step 'takes: slide upload, arm, record a take from its own camera page (660 Hz)'
api /api/takes/status | grep -q '"armed": false' || fail 'the takes plugin is not loaded'
timeout -k 10 60 docker exec "$id-obs" ffmpeg -nostdin -hide_banner -loglevel error -f lavfi \
    -i color=c=0x2060a0:size=800x450 -frames:v 1 -y /tmp/slide.png || fail 'could not make a slide picture'
slides="$(timeout -k 10 60 docker exec "$id-obs" curl -fsS --max-time 30 -F name=Slide -F file=@/tmp/slide.png \
    http://127.0.0.1:18095/api/takes/slides)" || fail 'POST /api/takes/slides'
deck="$(printf '%s' "$slides" | field 'd["slides"]["id"]')" || fail "unexpected upload response: $slides"
api /api/takes/arm -X POST -H 'Content-Type: application/json' \
    -d "{\"layout\": \"pip\", \"slide\": {\"deck_id\": \"$deck\", \"page\": 1}}" | grep -q '"armed": true' || fail 'POST /api/takes/arm'
started=false
for _ in $(seq 1 30); do   # the camera page needs a moment before its picture is not black
    if api_any /api/takes/start -X POST -H 'Content-Type: application/json' -d '{"take_id": "e2e-take"}' | grep -q '"take_id": "e2e-take"'; then
        started=true
        break
    fi
    sleep 2
done
[ "$started" = true ] || fail 'the take did not start'
echo 'while recording, the program must stay silent (the take camera feeds audio track 2 only)'
if desk tone 660 8 >/dev/null 2>&1; then fail 'the take camera tone leaked into the program audio'; fi
sleep 4
take="$(api /api/takes/stop -X POST)" || fail 'POST /api/takes/stop'
printf '%s' "$take" | grep -q '"has_audio": true' || { echo "$take" >&2; fail 'the take has no audio'; }

step 'assemble the take (trimmed) with the slide, leave take mode and play the result into the program'
job="$(api /api/takes/assemble -X POST -H 'Content-Type: application/json' -d "{\"name\": \"E2E take\", \"items\": [
    {\"type\": \"take\", \"take_id\": \"e2e-take\", \"start\": 1, \"end\": 9},
    {\"type\": \"still\", \"deck_id\": \"$deck\", \"page\": 1, \"seconds\": 1}]}")" || fail 'POST /api/takes/assemble'
job_id="$(printf '%s' "$job" | field 'd["job"]["job_id"]')"
state=running
for _ in $(seq 1 60); do
    result="$(api "/api/takes/jobs/$job_id")"
    state="$(printf '%s' "$result" | field 'd["job"]["state"]')"
    [ "$state" = running ] || break
    sleep 2
done
[ "$state" = done ] || { echo "$result" >&2; fail 'the assembly did not finish'; }
take_clip="$(printf '%s' "$result" | field 'd["job"]["clip"]["id"]')"
api /api/takes/disarm -X POST | grep -q '"armed": false' || fail 'POST /api/takes/disarm'
api "/api/clip/$take_clip/scene" -X POST | grep -q '"ok": true' || fail 'POST /api/clip/<take>/scene'
desk tone 660 120 || fail 'the prepared take did not reach the virtual microphone with its 660 Hz tone'
api_any /health | grep -q '"healthy": true' || fail 'the output is not healthy after the take'

step 'logs are clean'
if docker logs "$id-obs" 2>&1 | grep -E 'Traceback|Error in plugin'; then fail 'obs-control logged a traceback'; fi
docker exec "$id-desktop" grep -q 'listening on udp' /tmp/e2e/bridge.log || fail 'the bridge never reported listening'
echo "obs-control e2e passed (video sink: $video)"
