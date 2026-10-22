#!/usr/bin/env bash
# Behaviour tests of the work-browser launcher and open helper against a stub "Firefox" (a script that
# records its arguments and holds the profile lock). Needs only bash and coreutils, so it runs on
# any host as well as inside the consumer image (where the smoke test also adds a private D-Bus
# bus and, separately, the real Firefox).
#   launcher-test.sh [<component-dir>]    default: the directory above this script
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
component="${1:-$(cd "$here/.." && pwd)}"
launcher="$component/bin/malrunar-work-browser"
opener="$component/bin/malrunar-work-browser-open"
fail() { echo "launcher test failed: $*" >&2; exit 1; }

work="$(mktemp -d)"
launcher_pid=""
cleanup() {
    [ -z "$launcher_pid" ] || kill "$launcher_pid" 2>/dev/null || true
    pkill -f "$work/firefox-stub" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT

# The stub: log "<argv>", take the lock the way Firefox does (symlink "host:+pid"), then stay alive.
cat > "$work/firefox-stub" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case " $* " in *" -new-tab "*|*" -new-window "*) exit 0 ;; esac   # a handoff to the running instance returns at once
profile=""
while [ $# -gt 0 ]; do [ "$1" = -profile ] && profile="$2"; shift; done
if [ -n "$profile" ] && [ -z "${STUB_NO_LOCK:-}" ]; then ln -sfn "127.0.0.1:+$$" "$profile/lock"; fi
[ -z "${STUB_EXIT:-}" ] || exit "$STUB_EXIT"
exec sleep 300
STUB
chmod +x "$work/firefox-stub"

if [ "$(id -u)" = 0 ]; then
    # As root the launcher must switch to MALRUNAR_WORK_BROWSER_USER, or say what is missing.
    echo '-- started as root without MALRUNAR_WORK_BROWSER_USER: exits 78'
    status=0
    out="$("$launcher" 2>&1)" || status=$?
    { [ "$status" = 78 ] && grep -q MALRUNAR_WORK_BROWSER_USER <<< "$out"; } || fail "root without a user not reported: $status $out"
    echo '-- a user that does not exist: exits 78 and names the setting'
    status=0
    out="$(MALRUNAR_WORK_BROWSER_USER=no-such-user "$launcher" 2>&1)" || status=$?
    { [ "$status" = 78 ] && grep -q MALRUNAR_WORK_BROWSER_USER <<< "$out"; } || fail "unknown user not reported: $status $out"
    echo '-- root with a user: switches to that user before checking for Firefox'
    if getent passwd tester >/dev/null; then
        status=0
        out="$(MALRUNAR_WORK_BROWSER_USER=tester MALRUNAR_WORK_BROWSER_FIREFOX=/nonexistent/firefox "$launcher" 2>&1)" || status=$?
        { [ "$status" = 78 ] && grep -q desktop-base <<< "$out"; } || fail "root with a user: $status $out"
    fi
    echo 'launcher test (root) passed'
    exit 0
fi

export HOME="$work/home"
mkdir -p "$HOME"
export STUB_LOG="$work/stub.log"
export MALRUNAR_WORK_BROWSER_FIREFOX="$work/firefox-stub"
export MALRUNAR_WORK_BROWSER_DISPLAY=none
export MALRUNAR_WORK_BROWSER_DBUS="${MALRUNAR_WORK_BROWSER_DBUS:-none}"
export MALRUNAR_WORK_BROWSER_RUNTIME_DIR="$work/run"
export MALRUNAR_WORK_BROWSER_PROFILE_DIR="$work/profile"
export MALRUNAR_WORK_BROWSER_RETRY_SECONDS=1
unset MALRUNAR_WORK_BROWSER_USER DISPLAY DBUS_SESSION_BUS_ADDRESS

wait_for() { # <description> <command...>: poll for up to 20 s
    local what="$1"; shift
    for _ in $(seq 1 80); do "$@" && return 0; sleep 0.25; done
    fail "timed out waiting for $what"
}
stub_runs() { [ -f "$STUB_LOG" ] && [ "$(wc -l < "$STUB_LOG")" -ge "$1" ]; }
start_launcher() { "$launcher" > "$work/launcher.log" 2>&1 & launcher_pid=$!; }
stop_launcher() {
    kill "$launcher_pid"
    wait "$launcher_pid" 2>/dev/null || true
    launcher_pid=""
    pkill -f "$work/firefox-stub" 2>/dev/null || true
    rm -f "$STUB_LOG"
}

echo '-- no Firefox: exits 78 and names desktop-base'
status=0
out="$(MALRUNAR_WORK_BROWSER_FIREFOX="$work/does-not-exist" "$launcher" 2>&1)" || status=$?
[ "$status" = 78 ] || fail "expected exit 78 without Firefox, got $status"
grep -q 'desktop-base' <<< "$out" || fail "the message does not name desktop-base: $out"
status=0
out="$(MALRUNAR_WORK_BROWSER_FIREFOX="$work/does-not-exist" "$opener" 2>&1)" || status=$?
[ "$status" = 78 ] || fail "open helper: expected exit 78 without Firefox, got $status"

echo '-- MALRUNAR_WORK_BROWSER_ENABLED=0 exits 0 without starting Firefox'
MALRUNAR_WORK_BROWSER_ENABLED=0 "$launcher" > "$work/disabled.log" 2>&1 || fail 'ENABLED=0 did not exit 0'
[ ! -e "$STUB_LOG" ] || fail 'ENABLED=0 started Firefox'

echo '-- settings are validated with messages that name them'
status=0
out="$(MALRUNAR_WORK_BROWSER_USER_JS="$work/missing.js" "$launcher" 2>&1)" || status=$?
{ [ "$status" = 78 ] && grep -q MALRUNAR_WORK_BROWSER_USER_JS <<< "$out"; } || fail "bad USER_JS not reported: $status $out"
status=0
out="$(MALRUNAR_WORK_BROWSER_DBUS=bogus "$launcher" 2>&1)" || status=$?
{ [ "$status" = 78 ] && grep -q MALRUNAR_WORK_BROWSER_DBUS <<< "$out"; } || fail "bad DBUS not reported: $status $out"
rm -rf "$work/profile" "$STUB_LOG"

echo '-- profile from the consumer: seed copied without overwriting, user.js installed, arguments passed'
mkdir -p "$work/seed/chrome" "$work/profile"
echo 'seed' > "$work/seed/chrome/userChrome.css"
echo 'seed' > "$work/seed/existing.txt"
echo 'mine' > "$work/profile/existing.txt"
echo 'user_pref("browser.startup.page", 3);' > "$work/consumer-user.js"
export MALRUNAR_WORK_BROWSER_PROFILE_SEED="$work/seed"
export MALRUNAR_WORK_BROWSER_USER_JS="$work/consumer-user.js"
export MALRUNAR_WORK_BROWSER_ARGS='--foo --bar=baz'
export MALRUNAR_WORK_BROWSER_START_URLS='https://one.example https://two.example'
export MALRUNAR_WORK_BROWSER_PID_FILE="$work/state/browser.pid"
start_launcher
wait_for 'the stub to start' stub_runs 1
[ "$(head -1 "$STUB_LOG")" = "-profile $work/profile -new-instance --foo --bar=baz https://one.example https://two.example" ] \
    || fail "unexpected Firefox arguments: $(head -1 "$STUB_LOG")"
[ "$(cat "$work/profile/existing.txt")" = mine ] || fail 'the seed overwrote an existing profile file'
[ "$(cat "$work/profile/chrome/userChrome.css")" = seed ] || fail 'the seed was not copied'
cmp "$work/consumer-user.js" "$work/profile/user.js" || fail 'user.js was not installed from MALRUNAR_WORK_BROWSER_USER_JS'
[ "$(stat -c %a "$work/profile")" = 700 ] || fail 'profile directory is not mode 700'
[ -s "$work/state/browser.pid" ] || fail 'no pid file'
kill -0 "$(cat "$work/state/browser.pid")" || fail 'the pid file does not name a live process'

echo '-- open helper hands a URL to the running browser'
"$opener" https://three.example
[ "$(tail -1 "$STUB_LOG")" = "-profile $work/profile --headless -new-tab https://three.example" ] || fail "open helper arguments: $(tail -1 "$STUB_LOG")"
MALRUNAR_WORK_BROWSER_OPEN_MODE=window "$opener"
[ "$(tail -1 "$STUB_LOG")" = "-profile $work/profile --headless -new-window about:blank" ] || fail "open helper window mode: $(tail -1 "$STUB_LOG")"

echo '-- stopping the launcher stops Firefox and removes the pid file'
pid="$(cat "$work/state/browser.pid")"
kill "$launcher_pid"
wait "$launcher_pid" 2>/dev/null || true
launcher_pid=""
wait_for 'Firefox to stop' bash -c "! kill -0 $pid 2>/dev/null"
[ ! -e "$work/state/browser.pid" ] || fail 'pid file left behind'
rm -f "$STUB_LOG"

echo '-- open helper without a running browser exits 3'
status=0
"$opener" https://four.example > "$work/open.log" 2>&1 || status=$?
[ "$status" = 3 ] || fail "expected exit 3 with no browser running, got $status ($(cat "$work/open.log"))"

echo '-- a stale profile lock is cleared before the start'
ln -sfn '127.0.0.1:+999999' "$work/profile/lock"
start_launcher
wait_for 'the stub to start' stub_runs 1
grep -q 'stale profile lock' "$work/launcher.log" || fail "stale lock not reported: $(cat "$work/launcher.log")"
stop_launcher

echo '-- Firefox that exits is started again'
export STUB_EXIT=7
start_launcher
wait_for 'a restart' stub_runs 2
grep -q 'status 7' "$work/launcher.log" || fail "exit status not reported: $(cat "$work/launcher.log")"
unset STUB_EXIT
stop_launcher
echo 'launcher test passed'
