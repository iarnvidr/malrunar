#!/usr/bin/env bash
# Smoke test for the work-browser component: scripts/smoke.sh work-browser [image-ref]
# The component is FROM scratch, so it is checked from its file list and then exercised the way a
# consumer uses it: copied into a desktop built on desktop-base (test/consumer), with a desktop
# user, under supervisord, running the real Firefox headless. It also enforces the declared
# dependencies: without desktop-base the launcher must fail with a message that says so, and the
# component must not need node.
#
# Needs a desktop-base image: $DESKTOP_BASE_REF, else the local tag scripts/build.sh created.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "work-browser smoke failed: $*" >&2; exit 1; }
desktop_base="${DESKTOP_BASE_REF:-malrunar/desktop-base:h-$(python3 "$root/scripts/malrunar_image.py" hash desktop-base)}"
docker image inspect "$desktop_base" >/dev/null 2>&1 || fail "desktop-base image $desktop_base is not available locally (build it with scripts/build.sh desktop-base --load, or set DESKTOP_BASE_REF)"
ubuntu_image="$(python3 "$root/scripts/malrunar_image.py" pins --all work-browser | sed -n 's/^UBUNTU_IMAGE=//p')"
apt_snapshot="$(python3 "$root/scripts/malrunar_image.py" pins --all work-browser | sed -n 's/^APT_SNAPSHOT=//p')"
[ -n "$ubuntu_image" ] && [ -n "$apt_snapshot" ] || fail 'UBUNTU_IMAGE or APT_SNAPSHOT pin missing'

work="$(mktemp -d)"
cid=""
consumer="malrunar-test/work-browser-consumer:$$"
bare="malrunar-test/work-browser-bare:$$"
xvfb="malrunar-test/work-browser-xvfb:$$"
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    docker rmi -f "$xvfb" "$consumer" "$bare" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

echo '== files: scripts, the supervisord fragment and examples only; no Firefox, policies or preferences'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/work-browser/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/work-browser\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^etc/$\|^etc/supervisor/$\|^etc/supervisor/conf.d/$\|^etc/supervisor/conf.d/malrunar-work-browser.conf$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/work-browser and the supervisord fragment: $outside"
if grep -Ei 'firefox|policies\.json|distribution|mozilla\.cfg|autoconfig|\.xpi$|omni\.ja|(^|/)user\.js$|(^|/)prefs\.js$' "$work/files.txt" | grep -v 'opt/malrunar/work-browser/share/.*\.example$' | grep -v 'malrunar-work-browser' ; then
    fail 'the component ships Firefox, policies, extensions or preferences; it must run the unmodified Firefox of desktop-base'
fi
for f in bin/malrunar-work-browser bin/malrunar-work-browser-open; do
    test -x "$work/out/$f" || fail "$f is missing or not executable"
done
for f in README.md LICENSE lib/common.sh share/user.js.example share/policies.json.example share/Dockerfile.example; do
    test -s "$work/out/$f" || fail "$f is missing"
done
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" \
    || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-work-browser"
bash -n "$work/out/bin/malrunar-work-browser-open"
bash -n "$work/out/lib/common.sh"
python3 -I -c 'import json, sys; json.load(open(sys.argv[1]))' "$work/out/share/policies.json.example"

echo '== launcher behaviour on any host (stub Firefox)'
"$here/launcher-test.sh" "$work/out"

echo '== dependency: without desktop-base the launcher fails clearly and says what is missing'
docker build --quiet --tag "$bare" \
    --build-arg "WORK_BROWSER_IMAGE=$image" --build-arg "BASE=$ubuntu_image" "$here/consumer" >/dev/null
status=0
out="$(timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/tmp "$bare" \
    /opt/malrunar/work-browser/bin/malrunar-work-browser 2>&1)" || status=$?
echo "$out"
[ "$status" = 78 ] || fail "without desktop-base the launcher should exit 78, it exited $status"
grep -q 'desktop-base' <<< "$out" || fail 'the message without desktop-base does not name desktop-base'
status=0
out="$(timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/tmp "$bare" \
    /opt/malrunar/work-browser/bin/malrunar-work-browser-open https://example.invalid/ 2>&1)" || status=$?
[ "$status" = 78 ] || fail "without desktop-base the open helper should exit 78, it exited $status"

echo '== consumer image: desktop-base + the component, and nothing else (no node)'
docker build --quiet --tag "$consumer" \
    --build-arg "WORK_BROWSER_IMAGE=$image" --build-arg "BASE=$desktop_base" "$here/consumer" >/dev/null
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$@"; }
docker build --quiet --tag "$xvfb" --build-arg "CONSUMER=$consumer" --build-arg "APT_SNAPSHOT=$apt_snapshot" \
    --file "$here/consumer/xvfb.Dockerfile" "$here/consumer" >/dev/null
run "$consumer" bash -c '! command -v node >/dev/null && ! command -v npm >/dev/null' \
    || fail 'node is present in the test consumer; the component must not need it'

echo '== launcher behaviour inside desktop-base (as the desktop user, and as root)'
run -v "$here:/test:ro" --user 1000:1000 -e MALRUNAR_WORK_BROWSER_DBUS=private "$consumer" \
    /test/launcher-test.sh /opt/malrunar/work-browser || fail 'launcher test as the desktop user'
run -v "$here:/test:ro" --user 0:0 "$consumer" \
    /test/launcher-test.sh /opt/malrunar/work-browser || fail 'launcher test as root'

echo '== the supervisord program is part of the image and is picked up from conf.d'
run "$consumer" bash -c '
    set -e
    test -s /etc/supervisor/conf.d/malrunar-work-browser.conf
    grep -q "^files = /etc/supervisor/conf.d/\*.conf" /etc/supervisor/supervisord.conf
' || fail 'supervisord does not include conf.d or the fragment is missing'

echo '== under supervisord, with the real (unmodified) Firefox, headless, as the desktop user'
cat > "$work/user.js" <<'USERJS'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("app.update.auto", false);
USERJS
chmod 755 "$work"   # the desktop user (uid 1000) reads these through the mount
chmod 644 "$work/user.js"
cat > "$work/integration.sh" <<'INTEGRATION'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_WORK_BROWSER_USER=tester MALRUNAR_WORK_BROWSER_DISPLAY=none MALRUNAR_WORK_BROWSER_ARGS=--headless
export MALRUNAR_WORK_BROWSER_USER_JS=/work/user.js MALRUNAR_WORK_BROWSER_RETRY_SECONDS=2
profile=/home/tester/.config/malrunar-work-browser/profile
# appended, not truncated: the program logs to /dev/stdout, which is this file, through its own descriptor
supervisord -n -c /etc/supervisor/supervisord.conf >> /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; cat /tmp/supervisord.out' EXIT
state=''
for _ in $(seq 1 60); do
    state="$(supervisorctl -c /etc/supervisor/supervisord.conf status work-browser 2>&1 || true)"
    case "$state" in *RUNNING*) break ;; esac
    sleep 1
done
echo "$state"
case "$state" in *RUNNING*) ;; *) echo 'work-browser did not reach RUNNING'; exit 1 ;; esac
# Firefox is up on the profile: the lock is taken by a live firefox process of the desktop user
for _ in $(seq 1 60); do
    [ -L "$profile/lock" ] && break
    sleep 1
done
[ -L "$profile/lock" ] || { echo 'Firefox never took the profile lock'; ls -la "$profile" || true; exit 1; }
owner="$(readlink "$profile/lock")"; owner="${owner##*+}"
test "$(ps -o user= -p "$owner")" = tester
ps -o args= -p "$owner" | grep -q -- "/opt/malrunar/firefox/firefox"
cmp /work/user.js "$profile/user.js"
test "$(stat -c %U "$profile")" = tester
# the same state a minute later: it keeps running
sleep 5
supervisorctl -c /etc/supervisor/supervisord.conf status work-browser | grep -q RUNNING
supervisorctl -c /etc/supervisor/supervisord.conf stop work-browser
INTEGRATION
chmod 755 "$work/integration.sh"
run -v "$work:/work:ro" "$consumer" /work/integration.sh || fail 'supervised Firefox'

echo '== with a display (Xvfb): the launcher finds it, opens a window, and the open helper hands a URL to the running instance'
cat > "$work/display.sh" <<'DISPLAYTEST'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_WORK_BROWSER_USER=tester MALRUNAR_WORK_BROWSER_RETRY_SECONDS=2
profile=/home/tester/.config/malrunar-work-browser/profile
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > /tmp/xvfb.out 2>&1 &
supervisord -n -c /etc/supervisor/supervisord.conf >> /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; cat /tmp/xvfb.out /tmp/supervisord.out' EXIT
for _ in $(seq 1 90); do
    [ -L "$profile/lock" ] && break
    sleep 1
done
[ -L "$profile/lock" ] || { echo 'Firefox never took the profile lock'; exit 1; }
owner="$(readlink "$profile/lock")"; owner="${owner##*+}"
ps -o args= -p "$owner" | grep -q -- '/opt/malrunar/firefox/firefox'
# a browser window appears on the display the launcher found (it was not told which one)
for _ in $(seq 1 60); do
    xwininfo -display :77 -root -tree 2>/dev/null | grep -qi 'firefox' && break
    sleep 1
done
xwininfo -display :77 -root -tree | grep -qi 'firefox' || { echo 'no Firefox window on the display'; exit 1; }
tr '\0' '\n' < "/proc/$owner/environ" 2>/dev/null | grep -qx 'DISPLAY=:77' || { echo "the launcher did not export the display it found"; exit 1; }
out="$(/opt/malrunar/work-browser/bin/malrunar-work-browser-open about:blank 2>&1)" || { echo "open failed: $out"; exit 1; }
echo "malrunar-work-browser-open: ok ${out:+($out)}"
case "$out" in *'not responding'*|*'already running'*) echo 'the handoff did not reach the running instance'; exit 1 ;; esac
supervisorctl -c /etc/supervisor/supervisord.conf status work-browser | grep -q RUNNING
DISPLAYTEST
chmod 755 "$work/display.sh"
timeout -k 10 300 docker run --rm --entrypoint '' --shm-size 512m -v "$work:/work:ro" "$xvfb" /work/display.sh || fail 'supervised Firefox on a display'

echo '== Firefox is still exactly the one desktop-base installed'
run "$consumer" bash -c '
    set -e
    cd /opt/malrunar/firefox && LC_ALL=C sha256sum -c --quiet /usr/local/share/malrunar/desktop-base/firefox.sha256
    test "$(cd /opt/malrunar/firefox/distribution && find . -mindepth 1 | sort)" = "./distribution.ini"
    test ! -e /opt/malrunar/firefox/policies.json
    test ! -e /etc/firefox
    test -z "$(find / -xdev -name policies.json -path "*irefox*" 2>/dev/null)"
    test "$(find /opt/malrunar/firefox ! -user root | wc -l)" = 0
' || fail 'the Firefox installation differs from desktop-base, or a policies.json exists without the consumer adding one'
echo 'work-browser smoke passed'
