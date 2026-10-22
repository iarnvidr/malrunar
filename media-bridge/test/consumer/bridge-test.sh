#!/usr/bin/env bash
# Runs inside the test consumer container as an unprivileged user (uid 1000).
#   bridge-test.sh file               video to a file (the documented fallback sink)
#   bridge-test.sh v4l2 /dev/videoN   video to a v4l2loopback device mapped into the container
# Starts PipeWire without hardware, sends a generated MPEG-TS stream (colour test pattern and a
# 440 Hz sine) to the bridge and checks that the virtual microphone appears, is the default source
# and carries the tone, that video frames arrive at the sink, and that failures are reported clearly.
set -euo pipefail

mode="${1:?usage: bridge-test.sh file|v4l2 [device]}"
device="${2:-}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && command -v dbus-run-session >/dev/null; then
    exec dbus-run-session -- "$0" "$@"
fi

bridge=/opt/malrunar/media-bridge/bin/malrunar-media-bridge
port=15004
mic=smoke_mic
work="$(mktemp -d)"
XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
export HOME="${HOME:-/home/tester}"
fail() { echo "FAIL: $*" >&2; exit 1; }
pids=()
cleanup() {
    status=$?
    for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done
    if [ "$status" -ne 0 ]; then
        for log in "$work"/*.log; do echo "--- $log"; tail -n 40 "$log"; done 2>/dev/null || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

echo "== PipeWire, WirePlumber and pipewire-pulse without hardware"
mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d"
cp /usr/local/share/malrunar/desktop-base/examples/wireplumber-virtual-only.conf.example \
    "$HOME/.config/wireplumber/wireplumber.conf.d/virtual-only.conf"
pipewire >"$work/pipewire.log" 2>&1 & pids+=($!)
sleep 1
wireplumber >"$work/wireplumber.log" 2>&1 & pids+=($!)
pipewire-pulse >"$work/pipewire-pulse.log" 2>&1 & pids+=($!)
for _ in $(seq 1 30); do pactl info >/dev/null 2>&1 && break; sleep 1; done
pactl info | grep -E 'Server Name|Server Version'

echo "== bad configuration is refused with a message"
out="$(MALRUNAR_BRIDGE_SIZE=wide "$bridge" 2>&1)" && fail 'bad size accepted'
grep -q 'MALRUNAR_BRIDGE_SIZE' <<<"$out" || fail "bad size message: $out"
out="$(MALRUNAR_BRIDGE_VIDEO_SINK=nowhere "$bridge" 2>&1)" && fail 'bad sink accepted'
grep -q 'MALRUNAR_BRIDGE_VIDEO_SINK' <<<"$out" || fail "bad sink message: $out"

echo "== a missing video device gives a clear failure"
rc=0
out="$(MALRUNAR_BRIDGE_WAIT=2 MALRUNAR_BRIDGE_VIDEO_DEVICE=/dev/video99 "$bridge" 2>&1)" || rc=$?
echo "$out"
[ "$rc" -eq 3 ] || fail "missing device: expected exit 3, got $rc"
grep -q '/dev/video99 does not exist' <<<"$out" || fail 'missing device message lacks the device'
grep -q 'setup-v4l2loopback.sh' <<<"$out" || fail 'missing device message lacks the host setup hint'
grep -q 'VIDEO_SINK=file:' <<<"$out" || fail 'missing device message lacks the file fallback'

echo "== bridge ($mode sink)"
if [ "$mode" = v4l2 ]; then
    [ -c "$device" ] || fail "$device is not a character device in the container"
    sink_env=(MALRUNAR_BRIDGE_VIDEO_DEVICE="$device")
else
    sink_env=(MALRUNAR_BRIDGE_VIDEO_SINK="file:$work/video.yuv")
fi
env "${sink_env[@]}" MALRUNAR_BRIDGE_UDP_PORT="$port" MALRUNAR_BRIDGE_BIND=127.0.0.1 \
    MALRUNAR_BRIDGE_MIC_NAME="$mic" MALRUNAR_BRIDGE_MIC_DESCRIPTION='Smoke Test Microphone' \
    "$bridge" >"$work/bridge.log" 2>&1 & pids+=($!)
bridge_pid=$!
for _ in $(seq 1 30); do pactl list short sources | grep -q "[[:space:]]${mic}[[:space:]]" && break; sleep 1; done

echo "== virtual microphone in PipeWire"
pactl list short sources | grep "[[:space:]]${mic}[[:space:]]" || fail 'microphone missing from pactl sources'
pw-cli ls Node | grep -q "node.name = \"$mic\"" || fail 'microphone node missing from pw-cli'
description="$(pactl list sources | awk -v n="$mic" '$1=="Name:" {cur=$2} cur==n && $1=="Description:" {sub(/^[^:]*: /,""); print; exit}')"
[ "$description" = 'Smoke Test Microphone' ] || fail "microphone description is '$description'"
[ "$(pactl get-default-source)" = "$mic" ] || fail "default source is '$(pactl get-default-source)'"

echo "== test stream: testsrc video + 440 Hz sine, MPEG-TS over UDP"
ffmpeg -nostdin -hide_banner -loglevel error -re \
    -f lavfi -i 'testsrc=size=1280x720:rate=30' -f lavfi -i 'sine=frequency=440:sample_rate=48000' \
    -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -pix_fmt yuv420p -c:a aac -b:a 128k \
    -t 60 -f mpegts "udp://127.0.0.1:$port?pkt_size=1316" >"$work/sender.log" 2>&1 & pids+=($!)

echo "== the microphone carries the tone"
sleep 4
# 3 s of 48 kHz stereo s16le from the microphone
timeout 30 parec -d "$mic" --rate=48000 --channels=2 --format=s16le --raw | head -c 576000 > "$work/mic.raw"
python3 -I - "$work/mic.raw" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
assert len(data) == 576000, f"recorded {len(data)} bytes"
left = struct.unpack("<%dh" % (len(data) // 2), data)[0::2]
rate, window = 48000, 4800
rms = []
for start in range(0, len(left) - window + 1, window):
    chunk = left[start:start + window]
    rms.append((sum(v * v for v in chunk) / window) ** 0.5)
peak = max(rms)
loud = [r for r in rms if r > 0.5 * peak]
print(f"windows={len(rms)} peak-rms={peak:.0f} loud-windows={len(loud)}")
assert peak > 500, f"microphone is silent (peak rms {peak:.0f})"
assert len(loud) >= len(rms) // 2, "signal is intermittent"
# frequency from rising zero crossings in a loud stretch
best = max(range(0, len(left) - rate // 2, window), key=lambda s: sum(v * v for v in left[s:s + window]))
seg = left[best:best + rate // 2]
crossings = sum(1 for a, b in zip(seg, seg[1:]) if a < 0 <= b)
freq = crossings / 0.5
print(f"estimated frequency {freq:.0f} Hz")
assert 420 <= freq <= 460, f"expected about 440 Hz, got {freq}"
PY

echo "== video frames arrive"
frame=$(( 1280 * 720 * 3 / 2 ))
if [ "$mode" = v4l2 ]; then
    v4l2-ctl --device="$device" --info | sed 's/^/  /'
    timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f v4l2 -i "$device" -frames:v 15 \
        -f rawvideo -pix_fmt yuv420p "$work/frames.yuv"
    frames_file="$work/frames.yuv"; want=15
else
    for _ in $(seq 1 30); do
        [ "$(stat -c %s "$work/video.yuv" 2>/dev/null || echo 0)" -ge $(( frame * 30 )) ] && break
        sleep 1
    done
    frames_file="$work/video.yuv"; want=30
fi
size="$(stat -c %s "$frames_file")"
echo "video file: $size bytes, frame size $frame"
[ "$size" -ge $(( frame * want )) ] || fail "got $size bytes, expected at least $want frames of 1280x720 yuv420p ($((frame * want)) bytes)"
python3 -I - "$frames_file" "$frame" <<'PY'
import sys
path, frame = sys.argv[1], int(sys.argv[2])
with open(path, "rb") as handle:
    handle.seek(frame * 5)
    data = handle.read(frame)
luma = data[:1280 * 720]
distinct = len(set(luma[::97]))
print(f"distinct luma values in frame 5: {distinct}")
assert len(data) == frame, "short frame"
assert distinct > 30, "frame is blank or uniform"
PY

echo "== xrdp audio tune"
pactl load-module module-null-sink sink_name=xrdp-sink >/dev/null
MALRUNAR_BRIDGE_MIC_NAME="$mic" MALRUNAR_AUDIO_WAIT=5 /opt/malrunar/media-bridge/bin/malrunar-xrdp-audio-tune
[ "$(pactl get-default-sink)" = xrdp-sink ] || fail 'default sink was not set'
[ "$(pactl get-default-source)" = "$mic" ] || fail 'default source is not the bridge microphone'
pw-metadata -n settings 0 clock.force-rate | grep -q "value:'44100'" || fail 'clock.force-rate not applied'
pw-metadata -n settings 0 clock.force-quantum | grep -q "value:'4096'" || fail 'clock.force-quantum not applied'

echo "== bridge stops cleanly and removes the microphone"
kill -TERM "$bridge_pid"
for _ in $(seq 1 20); do kill -0 "$bridge_pid" 2>/dev/null || break; sleep 0.5; done
! kill -0 "$bridge_pid" 2>/dev/null || fail 'bridge did not stop on SIGTERM'
pactl list short sources | grep -q "[[:space:]]${mic}[[:space:]]" && fail 'microphone still present after stop'
echo "bridge-test ($mode) passed"
