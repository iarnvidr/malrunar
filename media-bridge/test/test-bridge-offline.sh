#!/usr/bin/env bash
# Runs on any host without ffmpeg, PipeWire or Docker: the bridge script is run against stub
# ffmpeg and pactl programs, and its log filter is fed canned lines.
#   test-bridge-offline.sh <directory with malrunar-media-bridge and malrunar-bridge-logfilter.sh>
# Covers: MALRUNAR_BRIDGE_VIDEO_SINK=null, the bounded file sink, authenticated start-output headers,
# rejected settings, and suppression of expected decoder noise while real errors still pass.
set -euo pipefail
bindir="$(cd "${1:?usage: test-bridge-offline.sh <bin directory>}" && pwd)"
bridge="$bindir/malrunar-media-bridge"
filter="$bindir/malrunar-bridge-logfilter.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

mkdir -p "$work/bin" "$work/pipe" "$work/out"
export PATH="$work/bin:$PATH" STUB_LOG="$work/ffmpeg-args.log"
cat > "$work/bin/pactl" <<'STUB'
#!/bin/sh
case "$1 $2" in
    "load-module module-pipe-source") echo 42 ;;
esac
exit 0
STUB
# ffmpeg stub: records its arguments, prints decoder noise plus one real error on stderr, and
# writes STUB_BYTES of data to the raw video output (if any).
cat > "$work/bin/ffmpeg" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
fmt=""; vout=""
while [ $# -gt 0 ]; do
    if [ "$1" = -f ]; then
        fmt="$2"
        case "$fmt" in rawvideo|null) vout="$3"; vfmt="$fmt" ;; esac
        shift
    fi
    shift
done
{
    echo '[h264 @ 0x55d0c8a1] non-existing PPS 0 referenced'
    echo '[h264 @ 0x55d0c8a1] decode_slice_header error'
    echo '[h264 @ 0x55d0c8a1] no frame!'
    echo '[vist#0:0/h264 @ 0x55d0c8a2] Error submitting packet to decoder: Invalid data found when processing input'
    echo 'udp://127.0.0.1:5004: Connection refused (injected real error)'
} >&2
if [ "${vfmt:-}" = rawvideo ]; then
    head -c "${STUB_BYTES:-0}" /dev/zero > "$vout"
fi
exit 0
STUB
chmod +x "$work/bin"/*

run_bridge() {   # run_bridge <logfile> [VAR=value ...]; exit status is the bridge's
    local log="$1"; shift
    : > "$STUB_LOG"
    env MALRUNAR_BRIDGE_RESTART=0 MALRUNAR_BRIDGE_WAIT=0 MALRUNAR_BRIDGE_PIPE_DIR="$work/pipe" \
        MALRUNAR_BRIDGE_UDP_PORT=15999 XDG_RUNTIME_DIR="$work/pipe" "$@" "$bridge" > "$log" 2>&1
}
size_of() { stat -c %s -- "$1" 2>/dev/null || echo 0; }

echo "== log filter (canned lines, no ffmpeg)"
canned="$(printf '%s\n' \
    '[h264 @ 0x1] non-existing PPS 0 referenced' \
    '[h264 @ 0x1] non-existing SPS 0 referenced' \
    '[h264 @ 0x1] decode_slice_header error' \
    '[h264 @ 0x1] no frame!' \
    '[vist#0:0/h264 @ 0x2] Error submitting packet to decoder: Invalid data found when processing input' \
    'Error while decoding stream #0:0: Invalid data found when processing input' \
    'udp://127.0.0.1:5004: Connection refused' \
    'Unknown encoder foo' \
    '[mpegts @ 0x3] Packet corrupt (stream = 0, dts = 1).')"
out="$(printf '%s\n' "$canned" | "$filter" 30)"
[ "$(wc -l <<<"$out")" -eq 3 ] || fail "filter kept $(wc -l <<<"$out") lines, expected the 3 real ones: $out"
grep -q 'Connection refused' <<<"$out" && grep -q 'Unknown encoder' <<<"$out" && grep -q 'Packet corrupt' <<<"$out" \
    || fail "filter dropped a real error: $out"
grep -qE 'PPS|SPS|no frame|Error submitting|decode_slice' <<<"$out" && fail "filter kept noise: $out"
pass "log filter drops known decoder noise and keeps real errors"
[ "$(printf '%s\n' "$canned" | "$filter" 0 | wc -l)" -eq 9 ] || fail 'quiet period 0 must pass everything'
pass "log filter with quiet seconds 0 passes everything"
out="$(printf 'frame=    0 fps=0.0\rno frame!\rframe=    5 fps=30\rno frame!\r\n' | "$filter" 30)"
[ "$(grep -c 'no frame!' <<<"$out")" -eq 1 ] || fail "first-frame progress did not end the quiet period: $out"
pass "log filter ends the quiet period at the first decoded frame"
out="$( { echo 'no frame!'; sleep 3; echo 'no frame!'; echo 'late real error'; } | "$filter" 1)"
[ "$(grep -c 'no frame!' <<<"$out")" -eq 1 ] && grep -q 'late real error' <<<"$out" || fail "quiet period did not expire: $out"
pass "log filter stops filtering after the quiet period"
if "$filter" abc </dev/null >/dev/null 2>&1; then fail 'filter accepted a bad quiet period'; fi

echo "== rejected configuration"
for bad in 0 -5 abc 1.5 '' 1000000000000000000; do
    rc=0; run_bridge "$work/bad.log" MALRUNAR_BRIDGE_VIDEO_SINK="file:$work/out/v.yuv" MALRUNAR_BRIDGE_FILE_MAX_BYTES="$bad" || rc=$?
    # an empty value falls back to the default, which is valid
    if [ -z "$bad" ]; then [ "$rc" -eq 0 ] || fail "empty MAX_BYTES should use the default: $(cat "$work/bad.log")"; continue; fi
    [ "$rc" -eq 2 ] || fail "MAX_BYTES='$bad' accepted (exit $rc)"
    grep -q 'MALRUNAR_BRIDGE_FILE_MAX_BYTES' "$work/bad.log" || fail "no message for MAX_BYTES='$bad': $(cat "$work/bad.log")"
done
pass "bad MALRUNAR_BRIDGE_FILE_MAX_BYTES is rejected (0, -5, abc, 1.5, huge)"
rc=0; run_bridge "$work/bad.log" MALRUNAR_BRIDGE_STARTUP_QUIET_SECONDS=soon || rc=$?
[ "$rc" -eq 2 ] && grep -q STARTUP_QUIET_SECONDS "$work/bad.log" || fail 'bad STARTUP_QUIET_SECONDS accepted'
rc=0; run_bridge "$work/bad.log" MALRUNAR_BRIDGE_VIDEO_SINK=nowhere || rc=$?
[ "$rc" -eq 2 ] || fail "unknown sink accepted (exit $rc)"
grep -q 'MALRUNAR_BRIDGE_VIDEO_SINK' "$work/bad.log" && grep -q "'null'" "$work/bad.log" || fail "unknown sink message does not name the choices: $(cat "$work/bad.log")"
pass "unknown video sink is rejected and the message lists null"

rm -f "$work/out"/*
echo "== null sink"
run_bridge "$work/null.log" MALRUNAR_BRIDGE_VIDEO_SINK=null STUB_BYTES=1048576 || fail "null sink run failed: $(cat "$work/null.log")"
grep -q -- '-f null -' "$STUB_LOG" || fail "ffmpeg was not told to decode into the null muxer: $(cat "$STUB_LOG")"
[ -z "$(ls -A "$work/out")" ] || fail "null sink stored something: $(ls -A "$work/out")"
pass "null sink: ffmpeg decodes into '-f null -', nothing stored"

echo "== bounded file sink"
size=320x180; frame=$(( 320 * 180 * 3 / 2 )); cap=1048576; segment=$(( cap / frame * frame ))
run_bridge "$work/file.log" MALRUNAR_BRIDGE_VIDEO_SINK="file:$work/out/v.yuv" MALRUNAR_BRIDGE_SIZE="$size" \
    MALRUNAR_BRIDGE_FILE_MAX_BYTES="$cap" STUB_BYTES=$(( 10 * segment + 3 * frame )) || fail "file sink run failed: $(cat "$work/file.log")"
a="$(size_of "$work/out/v.yuv")"; b="$(size_of "$work/out/v.yuv.1")"
echo "  v.yuv=$a v.yuv.1=$b cap=$cap segment=$segment (10 segments + 3 frames written)"
[ "$b" -gt 0 ] || fail 'no rotation happened'
[ "$a" -le "$cap" ] && [ "$b" -le "$cap" ] || fail "a segment exceeds the cap"
[ $(( a + b )) -le $(( 2 * cap )) ] || fail "total $((a + b)) exceeds 2 x cap"
[ "$b" -eq "$segment" ] && [ $(( a % frame )) -eq 0 ] || fail 'segments are not whole frames'
[ "$a" -eq $(( 3 * frame )) ] || fail "newest segment holds $a bytes, expected the last 3 frames ($(( 3 * frame )))"
[ "$(ls "$work/out" | wc -l)" -eq 2 ] || fail "unexpected files: $(ls "$work/out")"
pass "file sink bounded: wrote $(( 10 * segment + 3 * frame )) bytes, kept $a + $b bytes (cap $cap, at most 2 x cap on disk)"
# a cap smaller than one frame still keeps one whole frame
rm -f "$work/out"/*
run_bridge "$work/file.log" MALRUNAR_BRIDGE_VIDEO_SINK="file:$work/out/v.yuv" MALRUNAR_BRIDGE_SIZE="$size" \
    MALRUNAR_BRIDGE_FILE_MAX_BYTES=1000 STUB_BYTES=$(( 5 * frame )) || fail "tiny cap run failed"
[ "$(size_of "$work/out/v.yuv.1")" -eq "$frame" ] || fail 'cap below one frame did not keep whole frames'
pass "file sink bounded: cap below one frame rounds up to one frame"

echo "== startup noise"
run_bridge "$work/quiet.log" MALRUNAR_BRIDGE_VIDEO_SINK=null || fail "run failed: $(cat "$work/quiet.log")"
grep -qE 'non-existing PPS|no frame!|Error submitting packet|decode_slice_header' "$work/quiet.log" \
    && fail "expected decoder noise reached the log: $(cat "$work/quiet.log")"
grep -q 'Connection refused (injected real error)' "$work/quiet.log" || fail "real error was swallowed: $(cat "$work/quiet.log")"
pass "startup log is clean on mid-stream join and a real error still appears"
run_bridge "$work/loud.log" MALRUNAR_BRIDGE_VIDEO_SINK=null MALRUNAR_BRIDGE_STARTUP_QUIET_SECONDS=0 || fail 'run failed'
grep -q 'non-existing PPS 0 referenced' "$work/loud.log" || fail 'STARTUP_QUIET_SECONDS=0 should show the decoder messages'
pass "STARTUP_QUIET_SECONDS=0 turns the filter off"

echo '== authenticated start-output headers'
cat > "$work/bin/curl" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$CURL_LOG"
exit 0
STUB
chmod +x "$work/bin/curl"
export CURL_LOG="$work/curl.log"
printf 'Authorization: Bearer first-secret\nX-Test: one\n' > "$work/headers"
# Avoid the retry loop: the stub returns success immediately, but the bridge's one-second startup delay remains.
run_bridge "$work/auth.log" MALRUNAR_BRIDGE_VIDEO_SINK=null MALRUNAR_BRIDGE_START_OUTPUT_URL=http://controller.invalid/start MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE="$work/headers" || fail 'authenticated start request failed'
grep -q -- '-H Authorization: Bearer first-secret' "$CURL_LOG" || fail "auth header missing: $(cat "$CURL_LOG")"
grep -q -- '-H X-Test: one' "$CURL_LOG" || fail 'second header missing'
rc=0; run_bridge "$work/auth.log" MALRUNAR_BRIDGE_VIDEO_SINK=null MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE="$work/headers" || rc=$?
[ "$rc" -eq 2 ] && grep -q 'requires MALRUNAR_BRIDGE_START_OUTPUT_URL' "$work/auth.log" || fail 'headers file without URL accepted'
printf 'Authorization: Bearer bad\n\n-H https://evil.invalid\n' > "$work/headers"
: > "$CURL_LOG"
run_bridge "$work/auth.log" MALRUNAR_BRIDGE_VIDEO_SINK=null MALRUNAR_BRIDGE_START_OUTPUT_URL=http://controller.invalid/start MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE="$work/headers" || fail 'bridge failed after malformed header'
[ ! -s "$CURL_LOG" ] || fail 'malformed header request sent without authentication'
pass 'start-output headers parsed safely; file required with URL; malformed header never sent'

echo 'bridge offline tests passed'
