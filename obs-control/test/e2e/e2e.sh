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
#
# The Docker daemon may be remote (DOCKER_HOST, a remote context): nothing here mounts a path of the
# machine running this script. The test files reach the containers with `docker cp` between
# `docker create` and `docker start`, the desktop recipe is sent as a build context, and the only
# volume is a named one that lives on the daemon.
set -euo pipefail
obs_image="${1:?usage: e2e.sh <obs-control image> <media-bridge image> <desktop-base image> [/dev/videoN]}"
bridge_image="${2:?media-bridge component image}"
desktop_base="${3:?desktop-base image}"
v4l2_device="${4:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Self-check: this script must not bind-mount host paths (they do not exist on a remote daemon).
# The patterns are written with bracket expressions so that this check does not match itself.
if grep -nE '[-]v[ =]+"?(\$here|\$\{here|\$PWD|\$root|\$\(|/|\./|~)|[-][-]mount|[-][-]volume|type=[b]ind' "${BASH_SOURCE[0]}"; then
    echo 'obs-control e2e failed: the script bind-mounts a host path (not remote-daemon safe)' >&2
    exit 1
fi
echo 'E2E: remote-daemon-safe (no host bind mounts)'
root="$(cd "$here/../../.." && pwd)"
id="obs-control-e2e-$$"
consumer="malrunar-test/obs-control-e2e-desktop:$$"
# The bridge's file video sink is a bounded ring of two files (see desktop.sh); the cap is set explicitly.
file_max_bytes=33554432
fail() { echo "obs-control e2e failed: $*" >&2; exit 1; }
step() { echo "== $*"; }
[ -d "$root/media-bridge/test/consumer" ] || fail 'media-bridge/test/consumer is missing (the e2e reuses its desktop image recipe)'

secret_tar=""
cleanup() {
    status=$?
    [ -z "$secret_tar" ] || rm -f "$secret_tar"
    if [ "$status" -ne 0 ]; then
        for name in obs desktop; do
            echo "--- logs: $name" >&2
            docker logs "$id-$name" 2>&1 | tail -n 50 >&2 || true
        done
        echo '--- logs: obs-control (the OBS lines left out)' >&2
        docker logs "$id-obs" 2>&1 | grep -v -E '^(info|warning|debug): |obs-websocket|^\s' | tail -n 80 >&2 || true
        echo '--- bridge log' >&2
        docker exec "$id-desktop" tail -n 30 /tmp/e2e/bridge.log >&2 2>/dev/null || true
    fi
    docker rm -f "$id-obs" "$id-desktop" >/dev/null 2>&1 || true
    docker network rm "$id" >/dev/null 2>&1 || true
    docker volume rm "$id-media" >/dev/null 2>&1 || true
    docker rmi -f "$consumer" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# put_dir <container> <host dir> <absolute dir in the container>: copy a directory into a container
# that has been created but not started, with root ownership and world-readable modes (the files are
# used by an unprivileged user and the host's umask or owner must not matter).
put_dir() {
    tar -C "$2" --owner=0 --group=0 --mode='u+rwX,go+rX' --transform "s,^\./*,${3#/}/," -cf - . \
        | docker cp - "$1:/"
}

field() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
desk() { timeout -k 10 150 docker exec --user 1000:1000 "$id-desktop" /e2e/desktop.sh "$@"; }
# api <path> [curl args]: prints the response body on success. On an HTTP error status (or a curl
# failure) it prints the status and the response body to stderr and returns non-zero, so that a failing
# call says why (the server's JSON error) instead of only "returned error: 503".
trusted=(-H 'X-Forwarded-User: e2e-operator' -H 'X-Proxy-Secret: e2e-proxy-secret-0123456789')
api() {
    local out status
    out="$(timeout -k 10 60 docker exec "$id-obs" curl -sS --max-time 20 "${trusted[@]}" -w '\n%{http_code}' "http://127.0.0.1:18095$1" "${@:2}")" \
        || { echo "curl failed for $1: ${out:-no response}" >&2; return 1; }
    status="${out##*$'\n'}"
    out="${out%$'\n'*}"
    case "$status" in
        2??) printf '%s\n' "$out" ;;
        *) echo "HTTP $status from $1: $out" >&2; return 22 ;;
    esac
}
api_any() { timeout -k 10 60 docker exec "$id-obs" curl -sS --max-time 20 "${trusted[@]}" "http://127.0.0.1:18095$1" "${@:2}"; }
trusted=(-H 'X-Forwarded-User: e2e-operator' -H 'X-Proxy-Secret: e2e-proxy-secret-0123456789')

step 'desktop image: desktop-base + the media-bridge component'
docker build --quiet --tag "$consumer" --build-arg "BRIDGE_IMAGE=$bridge_image" \
    --build-arg "DESKTOP_BASE=$desktop_base" "$root/media-bridge/test/consumer" >/dev/null

step 'test media: video with a 1000 Hz tone (the scene "video"); the camera page carries 440 Hz'
# Docker chooses an isolated bridge subnet; proxy-secret trust does not depend on peer addressing.
docker network create --driver bridge "$id" >/dev/null
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
    # Probe the device on the daemon's host, not on this machine.
    device_gid="$(timeout -k 10 120 docker run --rm --entrypoint '' --device "$v4l2_device" "$consumer" \
        bash -c 'test -c "$1" && stat -c %g "$1"' _ "$v4l2_device")" || fail "$v4l2_device is not a character device on the Docker host"
    device_args=(--device "$v4l2_device" --group-add "$device_gid")
    video="$v4l2_device"
fi
timeout -k 10 120 docker create --name "$id-desktop" --network "$id" --network-alias desktop \
    --user 1000:1000 --entrypoint '' "${device_args[@]}" \
    -e "E2E_VIDEO=$video" -e E2E_START_URL=http://obs:18095/api/start-output \
    -e "E2E_FILE_MAX_BYTES=$file_max_bytes" \
    -e E2E_START_HEADERS=/tmp/e2e/headers "$consumer" dbus-run-session -- /e2e/desktop.sh serve >/dev/null
put_dir "$id-desktop" "$here" /e2e
timeout -k 10 120 docker start "$id-desktop" >/dev/null
docker exec "$id-desktop" sh -c 'chmod u+rwx /tmp/e2e; umask 077; : > /tmp/e2e/headers'
printf 'X-Forwarded-User: e2e-operator\nX-Proxy-Secret: e2e-proxy-secret-0123456789\n' | docker exec -i "$id-desktop" sh -c 'cat > /tmp/e2e/headers'
docker exec "$id-desktop" sh -c 'test -r /tmp/e2e/headers && test -s /tmp/e2e/headers && test -w /tmp/e2e/headers' || fail 'bridge auth header file is not readable/writable/populated'

step 'start obs-control, sending to the bridge'
timeout -k 10 120 docker create --name "$id-obs" --network "$id" --network-alias obs --shm-size 512m \
    -v "$id-media:/media:ro" \
    -e MALRUNAR_OBS_CONFIG=/e2e/obs-e2e.toml -e MALRUNAR_OBS_FEATURES_TAKES=true \
    -e MALRUNAR_OBS_CAMERA_URL=file:///usr/local/share/malrunar/obs-control/examples/test-card.html \
    -e MALRUNAR_OBS_MEDIA_FILE=/media/e2e.mp4 -e MALRUNAR_OBS_START_SCENE=video \
    -e MALRUNAR_OBS_BROLL_ENABLED=false \
    -e 'MALRUNAR_OBS_OUTPUT_URL=udp://desktop:5004?pkt_size=1316' \
    "$obs_image" >/dev/null
put_dir "$id-obs" "$here" /e2e
# Replace generic directory-copy metadata with the exact secret ownership/mode before startup.
# Validate the archive locally: docker exec cannot run against a CREATED container.
secret_tar="$(mktemp)"
tar -C "$here" --owner=1000 --group=1000 --mode=0600 --transform='s,^,e2e/,' -cf "$secret_tar" proxy-secret
python3 - "$secret_tar" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1], "r:") as archive:
    members = [m for m in archive.getmembers() if m.name == "e2e/proxy-secret"]
    assert len(members) == 1, "proxy secret archive member is missing or duplicated"
    member = members[0]
    assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o600), (
        f"proxy secret archive metadata is uid={member.uid} gid={member.gid} mode={member.mode:o}"
    )
PY
[ "$?" -eq 0 ] || fail 'proxy secret archive ownership/mode is incorrect'
docker cp - "$id-obs:/" < "$secret_tar"
rm -f "$secret_tar"
secret_tar=""
timeout -k 10 120 docker start "$id-obs" >/dev/null
ready=false
for _ in $(seq 1 120); do
    if api /ready 2>/dev/null | grep -q '"ready": true'; then ready=true; break; fi
    docker inspect -f '{{.State.Running}}' "$id-obs" 2>/dev/null | grep -q true || break
    sleep 2
done
[ "$ready" = true ] || fail 'obs-control did not become ready'

step 'access: authenticate remote and loopback operator requests, reject missing/wrong secrets'
# Validate the config the process actually loaded, then exercise real API middleware from both peers.
docker exec "$id-obs" env PYTHONPATH=/opt/malrunar/obs-control/lib python3 -c 'from malrunar_obs.config import load_config; c=load_config(); assert c.access.proxy_secret_header == "X-Proxy-Secret" and not c.access.trusted_proxies; assert c.identity_config().mode == "secret"'
for peer in loopback remote; do
    if [ "$peer" = loopback ]; then who="$id-obs"; url=http://127.0.0.1:18095/api/clips; else who="$id-desktop"; url=http://obs:18095/api/clips; fi
    timeout -k 10 60 docker exec "$who" curl -fsS --max-time 20 -H 'X-Forwarded-User: e2e-operator' -H 'X-Proxy-Secret: e2e-proxy-secret-0123456789' "$url" >/dev/null || fail "authenticated $peer request rejected"
    for secret in absent wrong; do
        args=(-H 'X-Forwarded-User: e2e-operator')
        [ "$secret" = absent ] || args+=(-H 'X-Proxy-Secret: wrong-secret-value-0123456789')
        status="$(docker exec "$who" curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "${args[@]}" "$url")"
        [ "$status" = 403 ] || fail "$peer $secret-secret request returned HTTP $status"
    done
done

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
upload="$(timeout -k 10 120 docker exec "$id-obs" curl -fsS --max-time 100 "${trusted[@]}" -F name='E2E clip' -F file=@/media/e2e.mp4 http://127.0.0.1:18095/api/clips/upload)" \
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

step 'takes: slide upload, then 10 event-verified arm/record/stop/assemble/disarm cycles (660 Hz)'
api /api/takes/status | grep -q '"armed": false' || fail 'the takes plugin is not loaded'
# OBS browser sources load asynchronously. The camera page may render black until the take
# scene is active, so only inspect camera_ready after arm; wait on the real signal, not a delay.
wait_take_camera_ready() {
    local status ready=false
    for _ in $(seq 1 120); do
        status="$(api /api/takes/status)" || fail 'could not read takes status while waiting for camera'
        if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("camera_ready") is True else 1)' <<<"$status"; then
            ready=true
            break
        fi
        docker inspect -f '{{.State.Running}}' "$id-obs" 2>/dev/null | grep -q true || break
        sleep 1
    done
    [ "$ready" = true ] || fail "take camera did not become ready; last status: $status"
}
timeout -k 10 60 docker exec "$id-obs" ffmpeg -nostdin -hide_banner -loglevel error -f lavfi \
    -i color=c=0x2060a0:size=800x450 -frames:v 1 -y /tmp/slide.png || fail 'could not make a slide picture'
slides="$(timeout -k 10 60 docker exec "$id-obs" curl -fsS --max-time 30 -F name=Slide -F file=@/tmp/slide.png "${trusted[@]}" \
    http://127.0.0.1:18095/api/takes/slides)" || fail 'POST /api/takes/slides'
deck="$(printf '%s' "$slides" | field 'd["slides"]["id"]')" || fail "unexpected upload response: $slides"
for cycle in $(seq 1 10); do
    take_id="e2e-take-$cycle"
    step "cycle $cycle/10: arm and record"
    api /api/takes/arm -X POST -H 'Content-Type: application/json' \
        -d "{\"layout\": \"pip\", \"slide\": {\"deck_id\": \"$deck\", \"page\": 1}}" | grep -q '"armed": true' || fail 'POST /api/takes/arm'
    wait_take_camera_ready
    api /api/takes/start -X POST -H 'Content-Type: application/json' -d "{\"take_id\": \"$take_id\"}" \
        | grep -q "\\\"take_id\\\": \\\"$take_id\\\"" || fail "POST /api/takes/start ($take_id)"
    # Give OBS a short real recording interval; stop is then verified against the finalized media.
    sleep 2
    desk silence 3 || fail "program was not silent while recording ($take_id)"
    take="$(api /api/takes/stop -X POST)" || fail "POST /api/takes/stop ($take_id)"
    printf '%s' "$take" | grep -q '"has_audio": true' || { echo "$take" >&2; fail "take has no audio ($take_id)"; }
    job="$(api /api/takes/assemble -X POST -H 'Content-Type: application/json' -d "{\"name\": \"E2E take $cycle\", \"items\": [{\"type\": \"take\", \"take_id\": \"$take_id\", \"start\": 0.5, \"end\": 1.5}, {\"type\": \"still\", \"deck_id\": \"$deck\", \"page\": 1, \"seconds\": 1}]}")" || fail "POST /api/takes/assemble ($take_id)"
    job_id="$(printf '%s' "$job" | field 'd["job"]["job_id"]')"
    state=running
    for _ in $(seq 1 60); do
        result="$(api "/api/takes/jobs/$job_id")"
        state="$(printf '%s' "$result" | field 'd["job"]["state"]')"
        [ "$state" = running ] || break
        sleep 2
    done
    [ "$state" = "done" ] || { echo "$result" >&2; fail "assembly failed ($take_id)"; }
    take_clip="$(printf '%s' "$result" | field 'd["job"]["clip"]["id"]')"
    api /api/takes/disarm -X POST | grep -q '"armed": false' || fail "POST /api/takes/disarm ($take_id)"
    api "/api/clip/$take_clip/scene" -X POST | grep -q '"ok": true' || fail 'POST /api/clip/<take>/scene'
    desk tone 660 30 || fail "prepared take audio failed ($take_id)"
    api_any /health | grep -q '"healthy": true' || fail 'the output is not healthy after the take'
done

if [ "$video" = file ]; then
    step 'the bounded video sink ring (video.yuv and video.yuv.1) is within the cap'
    # The bridge rotates the file sink: the segment being written and the previous one, each at
    # most the cap. Right after a rotation the current one can be tiny or empty.
    sizes="$(docker exec "$id-desktop" sh -c 'for f in /tmp/e2e/video.yuv /tmp/e2e/video.yuv.1; do if [ -e "$f" ]; then stat -c %s "$f"; else echo 0; fi; done')" \
        || fail 'could not stat the video sink files'
    cur_size="$(echo "$sizes" | sed -n 1p)"
    prev_size="$(echo "$sizes" | sed -n 2p)"
    echo "video sink: video.yuv $cur_size bytes, video.yuv.1 $prev_size bytes (cap $file_max_bytes each)"
    [ $(( cur_size + prev_size )) -gt 0 ] || fail 'both video sink files are empty or missing'
    [ "$cur_size" -le "$file_max_bytes" ] || fail "video.yuv ($cur_size bytes) exceeds the cap"
    [ "$prev_size" -le "$file_max_bytes" ] || fail "video.yuv.1 ($prev_size bytes) exceeds the cap"
fi

step 'the take cycles leave controller state clean'
ready=false
for _ in $(seq 1 120); do
    if docker exec "$id-obs" curl -fsS --max-time 5 "${trusted[@]}" http://127.0.0.1:18095/ready 2>/dev/null | grep -q '"ready": true'; then ready=true; break; fi
    sleep 2
done
[ "$ready" = true ] || fail 'authenticated /ready failed after controller restart'
status="$(timeout -k 10 60 docker exec "$id-obs" curl -fsS --max-time 20 "${trusted[@]}" http://127.0.0.1:18095/api/takes/status)" || fail 'trusted operator could not read takes status after restart'
printf '%s' "$status" | grep -q '"armed": false' || fail 'take mode was not reset on controller restart'

step 'logs are clean'
if docker logs "$id-obs" 2>&1 | grep -E 'Traceback|Error in plugin'; then fail 'obs-control logged a traceback'; fi
docker exec "$id-desktop" grep -q 'listening on udp' /tmp/e2e/bridge.log || fail 'the bridge never reported listening'
echo "obs-control e2e passed (video sink: $video)"
