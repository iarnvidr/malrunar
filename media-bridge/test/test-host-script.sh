#!/usr/bin/env bash
# Tests host/setup-v4l2loopback.sh against a fake sysfs, fake /dev and stubbed modprobe/fuser/sudo:
#   test-host-script.sh <path to setup-v4l2loopback.sh>
set -euo pipefail
script="$(cd "$(dirname "${1:?usage: test-host-script.sh <setup-v4l2loopback.sh>}")" && pwd)/$(basename "$1")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fail() { echo "host script test failed: $*" >&2; exit 1; }

mkdir -p "$work/bin" "$work/sys/class/video4linux" "$work/dev" "$work/etc"
export MALRUNAR_SYSFS="$work/sys" MALRUNAR_DEVDIR="$work/dev" MALRUNAR_CONFDIR="$work/etc"
export PATH="$work/bin:$PATH"
export STUB_LOG="$work/calls.log"
: > "$STUB_LOG"
printf '#!/bin/sh\nexec "$@"\n' > "$work/bin/sudo"
# modprobe stub: "-r" removes the fake module; otherwise creates the fake devices from the arguments.
cat > "$work/bin/modprobe" <<'STUB'
#!/usr/bin/env bash
echo "modprobe $*" >> "$STUB_LOG"
rm -rf "$MALRUNAR_SYSFS/class/video4linux"/* "$MALRUNAR_SYSFS/module/v4l2loopback" "$MALRUNAR_DEVDIR"/video*
[ "$1" = -r ] && exit 0
for arg in "$@"; do
    case "$arg" in
        video_nr=*) IFS=, read -ra nrs <<<"${arg#video_nr=}" ;;
        card_label=*) IFS=, read -ra names <<<"${arg#card_label=}" ;;
    esac
done
mkdir -p "$MALRUNAR_SYSFS/module/v4l2loopback"; echo 0 > "$MALRUNAR_SYSFS/module/v4l2loopback/refcnt"
for i in "${!nrs[@]}"; do
    mkdir -p "$MALRUNAR_SYSFS/class/video4linux/video${nrs[$i]}"
    printf '%s\n' "${names[$i]}" > "$MALRUNAR_SYSFS/class/video4linux/video${nrs[$i]}/name"
    mkfifo "$MALRUNAR_DEVDIR/video${nrs[$i]}"   # not a character device: the script only lists real ones via -c
done
STUB
printf '#!/bin/sh\n[ -n "${STUB_BUSY:-}" ] && [ "$1" = "$STUB_BUSY" ]\n' > "$work/bin/fuser"
chmod +x "$work/bin"/*
run() { "$script" "$@" 2>&1; }

echo '1. fresh host: loads the module with the right options'
out="$(run -n 2 -f 20 -l Cam)"; echo "$out"
grep -q 'modprobe v4l2loopback devices=2 video_nr=20,21 card_label=Cam 1,Cam 2 exclusive_caps=1,1' "$STUB_LOG" || fail 'wrong modprobe arguments'

echo '2. second run: nothing to do'
: > "$STUB_LOG"
out="$(run -n 2 -f 20 -l Cam)"; echo "$out"
grep -q 'already present' <<<"$out" || fail 'not reported as present'
[ ! -s "$STUB_LOG" ] || fail 'modprobe ran again'

echo '3. different layout while the module is in use: refuses, changes nothing'
echo 2 > "$work/sys/module/v4l2loopback/refcnt"
rc=0; out="$(run -n 1 -f 30 -l Other)" || rc=$?; echo "$out"
[ "$rc" -eq 1 ] || fail "expected exit 1, got $rc"
grep -q 'refusing to unload' <<<"$out" || fail 'no refusal message'
[ ! -s "$STUB_LOG" ] || fail 'modprobe ran although the module is in use'

echo '4. different layout, idle: reloads'
echo 0 > "$work/sys/module/v4l2loopback/refcnt"
out="$(run -n 1 -f 30 -l Other)"; echo "$out"
grep -q '^modprobe -r v4l2loopback' "$STUB_LOG" || fail 'did not unload'
grep -q 'modprobe v4l2loopback devices=1 video_nr=30 card_label=Other exclusive_caps=1' "$STUB_LOG" || fail 'did not reload with the new layout'

echo '5. --persist writes both files once'
out="$(run -n 1 -f 30 -l Other --persist)"; echo "$out"
[ "$(cat "$work/etc/modules-load.d/malrunar-media-bridge.conf")" = v4l2loopback ] || fail 'modules-load.d content'
grep -q '^options v4l2loopback devices=1 video_nr=30 card_label="Other" exclusive_caps=1$' "$work/etc/modprobe.d/malrunar-media-bridge.conf" || fail 'modprobe.d content'
out="$(run -n 1 -f 30 -l Other --persist)"
grep -q 'is up to date' <<<"$out" || fail 'persist is not idempotent'

echo '6. input validation'
for bad in '-n 0' '-n 99' '-f x' '-l a,b' '-n 2 -l a -l b -l c'; do
    # shellcheck disable=SC2086
    if "$script" $bad >/dev/null 2>&1; then fail "accepted: $bad"; fi
done
echo 'host script tests passed'
