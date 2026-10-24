#!/usr/bin/env bash
# Runs inside the desktop container (desktop-base + media-bridge) as an unprivileged user.
#   desktop.sh serve                  PipeWire without hardware, then the media-bridge in the foreground
#   desktop.sh tone <hz> <seconds>    wait until the virtual microphone carries a tone near <hz>
#   desktop.sh frame <name>           save the newest video frame as /tmp/e2e/<name>.yuv
#   desktop.sh diff <a> <b>           mean absolute difference of two saved frames (0-255)
#   desktop.sh silence <seconds>        assert the virtual microphone has no audible signal
# Settings (environment): E2E_MIC, E2E_SIZE (WxH), E2E_FPS, E2E_UDP_PORT, E2E_VIDEO (file|<device>),
# E2E_START_URL.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
export HOME="${HOME:-/home/tester}"
mic="${E2E_MIC:-e2e_mic}"
size="${E2E_SIZE:-320x180}"
fps="${E2E_FPS:-10}"
video="${E2E_VIDEO:-file}"
work=/tmp/e2e
mkdir -p "$work"
width="${size%x*}"
height="${size#*x}"
frame_bytes=$(( width * height * 3 / 2 ))

case "${1:?command}" in
serve)
    mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d"
    cp /usr/local/share/malrunar/desktop-base/examples/wireplumber-virtual-only.conf.example \
        "$HOME/.config/wireplumber/wireplumber.conf.d/virtual-only.conf"
    pids=()
    trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT TERM INT
    pipewire >"$work/pipewire.log" 2>&1 & pids+=($!)
    sleep 1
    wireplumber >"$work/wireplumber.log" 2>&1 & pids+=($!)
    pipewire-pulse >"$work/pipewire-pulse.log" 2>&1 & pids+=($!)
    for _ in $(seq 1 30); do pactl info >/dev/null 2>&1 && break; sleep 1; done
    if [ "$video" = file ]; then sink="file:$work/video.yuv"; else sink=v4l2; fi
    MALRUNAR_BRIDGE_UDP_PORT="${E2E_UDP_PORT:-5004}" MALRUNAR_BRIDGE_BIND=0.0.0.0 \
        MALRUNAR_BRIDGE_FILE_MAX_BYTES="${E2E_FILE_MAX_BYTES:-33554432}" \
        MALRUNAR_BRIDGE_VIDEO_SINK="$sink" MALRUNAR_BRIDGE_VIDEO_DEVICE="${video/file//dev/video10}" \
        MALRUNAR_BRIDGE_SIZE="$size" MALRUNAR_BRIDGE_FPS="$fps" \
        MALRUNAR_BRIDGE_MIC_NAME="$mic" MALRUNAR_BRIDGE_MIC_DESCRIPTION='E2E Microphone' \
        MALRUNAR_BRIDGE_START_OUTPUT_URL="${E2E_START_URL:-}" MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE="${E2E_START_HEADERS:-}" MALRUNAR_BRIDGE_LOGLEVEL=info \
        /opt/malrunar/media-bridge/bin/malrunar-media-bridge 2>&1 | tee "$work/bridge.log" &
    pids+=($!)
    wait
    ;;
tone)
    want="${2:?hz}"
    deadline=$(( SECONDS + ${3:-60} ))
    last=''
    while [ "$SECONDS" -lt "$deadline" ]; do
        if pactl list short sources 2>/dev/null | grep -q "[[:space:]]${mic}[[:space:]]"; then
            # 3 s of 48 kHz stereo s16le
            timeout 20 parec -d "$mic" --rate=48000 --channels=2 --format=s16le --raw 2>/dev/null | head -c 576000 > "$work/mic.raw" || true
            if last="$(python3 -I "$here/tone.py" "$work/mic.raw" "$want")"; then
                echo "tone near $want Hz: $last"
                exit 0
            fi
        else
            last='microphone not present yet'
        fi
        sleep 1
    done
    echo "no tone near $want Hz within ${3:-60}s (last: $last)" >&2
    exit 1
    ;;
silence)
    seconds="${2:-3}"
    timeout 20 parec -d "$mic" --rate=48000 --channels=2 --format=s16le --raw 2>/dev/null | head -c "$(( seconds * 48000 * 4 ))" > "$work/mic.raw" || true
    if last="$(python3 -I "$here/tone.py" "$work/mic.raw" 440)"; then
        echo "unexpected program audio during recording: $last" >&2
        exit 1
    fi
    grep -q '^silent ' <<<"$last" || { echo "could not verify silence: $last" >&2; exit 1; }
    echo "program silent ($last)"
    ;;
frame)
    name="${2:?name}"
    if [ "$video" = file ]; then
        # The bridge keeps a ring of two files: video.yuv (being written) and video.yuv.1 (the
        # previous full segment, a whole number of frames). Right after a rotation video.yuv is
        # short, so the newest frames are the tail of video.yuv.1 followed by video.yuv.
        file="$work/video.yuv"
        prev="$file.1"
        cur_count=$(( $(stat -c %s "$file" 2>/dev/null || echo 0) / frame_bytes ))
        prev_count=0
        if [ -f "$prev" ]; then prev_count=$(( $(stat -c %s "$prev") / frame_bytes )); fi
        count=$(( cur_count + prev_count ))
        [ "$cur_count" -ge 5 ] || [ "$count" -ge 5 ] || { echo "only $count frames in $file and $prev" >&2; exit 1; }
        # the second-newest frame: the newest may still be incomplete in the pipe
        want=$(( count - 2 ))
        if [ "$want" -ge "$prev_count" ]; then
            src="$file"; index=$(( want - prev_count ))
        else
            src="$prev"; index="$want"
        fi
        dd if="$src" of="$work/$name.yuv" bs="$frame_bytes" skip="$index" count=1 status=none
    else
        timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f v4l2 -i "$video" -frames:v 3 \
            -f rawvideo -pix_fmt yuv420p "$work/$name.all"
        tail -c "$frame_bytes" "$work/$name.all" > "$work/$name.yuv"
        rm -f "$work/$name.all"
    fi
    test "$(stat -c %s "$work/$name.yuv")" -eq "$frame_bytes"
    python3 -I - "$work/$name.yuv" "$width" "$height" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
luma = data[: int(sys.argv[2]) * int(sys.argv[3])]
print(f"frame: mean luma {sum(luma) / len(luma):.1f}, {len(set(luma[::7]))} distinct luma values")
PY
    ;;
diff)
    python3 -I - "$work/${2:?a}.yuv" "$work/${3:?b}.yuv" "$width" "$height" <<'PY'
import sys
a, b = open(sys.argv[1], "rb").read(), open(sys.argv[2], "rb").read()
n = int(sys.argv[3]) * int(sys.argv[4])
diff = sum(abs(x - y) for x, y in zip(a[:n], b[:n])) / n
print(f"{diff:.1f}")
PY
    ;;
*)
    echo "unknown command $1" >&2
    exit 2
    ;;
esac
