#!/usr/bin/env bash
# Smoke test for the session-kit component: scripts/smoke.sh session-kit [image-ref]
# The component is FROM scratch, so it is checked from its file list and then exercised the way a
# consumer uses it: copied into a desktop built on desktop-base (test/consumer). It also enforces the
# declared dependencies: without desktop-base the command fails with a message naming it; without
# xdotool the workspace tools say so; without work-browser the tools that need it say so (and the rest
# works); and nothing else (node, a browser, a window manager of the kit's own) is in the test image.
#
# Needs a desktop-base image ($DESKTOP_BASE_REF, else the local tag scripts/build.sh created) and a
# work-browser image ($WORK_BROWSER_REF, else its local tag) for the "present" half of the gating test.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "session-kit smoke failed: $*" >&2; exit 1; }
tool="$root/scripts/malrunar_image.py"
desktop_base="${DESKTOP_BASE_REF:-malrunar/desktop-base:h-$(python3 "$tool" hash desktop-base)}"
work_browser="${WORK_BROWSER_REF:-malrunar/work-browser:h-$(python3 "$tool" hash work-browser)}"
docker image inspect "$desktop_base" >/dev/null 2>&1 || fail "desktop-base image $desktop_base is not available locally (build it with scripts/build.sh desktop-base --load, or set DESKTOP_BASE_REF)"
docker image inspect "$work_browser" >/dev/null 2>&1 || fail "work-browser image $work_browser is not available locally (build it with scripts/build.sh work-browser --load, or set WORK_BROWSER_REF)"
ubuntu_image="$(python3 "$tool" pins --all session-kit | sed -n 's/^UBUNTU_IMAGE=//p')"
apt_snapshot="$(python3 "$tool" pins --all session-kit | sed -n 's/^APT_SNAPSHOT=//p')"
[ -n "$ubuntu_image" ] && [ -n "$apt_snapshot" ] || fail 'UBUNTU_IMAGE or APT_SNAPSHOT pin missing'

work="$(mktemp -d)"
cid=""
bare="malrunar-test/session-kit-bare:$$"
consumer="malrunar-test/session-kit-consumer:$$"
withx="malrunar-test/session-kit-x:$$"
withwb="malrunar-test/session-kit-wb:$$"
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    docker rmi -f "$withwb" "$withx" "$consumer" "$bare" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
chmod 755 "$work"   # the desktop user (uid 1000) reads the mounted files

echo '== files: the kit and its supervisord fragment only'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/session-kit/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/session-kit\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^etc/$\|^etc/supervisor/$\|^etc/supervisor/conf.d/$\|^etc/supervisor/conf.d/malrunar-session-kit.conf$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/session-kit and the supervisord fragment: $outside"
if grep -Ei 'firefox|policies\.json|\.xpi$|omni\.ja|xdotool$|node_modules|(^|/)node$' "$work/files.txt"; then
    fail 'the component ships a browser, a node runtime or another component'
fi
test -x "$work/out/bin/malrunar-session" || fail 'bin/malrunar-session is missing or not executable'
for f in README.md LICENSE lib/common.sh lib/session_kit.py share/Dockerfile.example; do
    test -s "$work/out/$f" || fail "$f is missing"
done
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" \
    || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-session"
bash -n "$work/out/lib/common.sh"

echo '== dependency: without desktop-base the command fails clearly and says what is missing'
docker build --quiet --tag "$bare" \
    --build-arg "SESSION_KIT_IMAGE=$image" --build-arg "BASE=$ubuntu_image" "$here/consumer" >/dev/null
status=0
out="$(timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/tmp "$bare" \
    /opt/malrunar/session-kit/bin/malrunar-session check 2>&1)" || status=$?
echo "$out"
[ "$status" = 78 ] || fail "without desktop-base the command should exit 78, it exited $status"
grep -q 'desktop-base' <<< "$out" || fail 'the message without desktop-base does not name desktop-base'

echo '== consumer image: desktop-base + the component, and nothing else'
docker build --quiet --tag "$consumer" \
    --build-arg "SESSION_KIT_IMAGE=$image" --build-arg "BASE=$desktop_base" "$here/consumer" >/dev/null
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$@"; }
run "$consumer" bash -c '! command -v node >/dev/null && ! command -v npm >/dev/null && ! command -v xdotool >/dev/null && ! test -e /opt/malrunar/work-browser' \
    || fail 'node, xdotool or work-browser is present in the test consumer; the component must not need them'

echo '== unit tests inside desktop-base (as the desktop user; the root path is exercised below)'
run -v "$here:/opt/malrunar/session-kit/test:ro" --user 1000:1000 -e HOME=/home/tester "$consumer" \
    python3 -m unittest discover -s /opt/malrunar/session-kit/test -v 2>&1 | tail -n 15 || fail 'unit tests as the desktop user'

echo '== without xdotool and without work-browser: each tool says what it needs, the rest works'
cat > "$work/gating.sh" <<'GATING'
#!/usr/bin/env bash
set -uo pipefail
kit=/opt/malrunar/session-kit/bin/malrunar-session
export MALRUNAR_SESSION_KIT_USER=tester
expect() {   # expect <status> <text in output> -- command...
    local want="$1" text="$2"; shift 3
    local out status=0
    out="$("$@" 2>&1)" || status=$?
    echo "$ $* -> $status: $out"
    [ "$status" = "$want" ] || { echo "expected exit status $want"; exit 1; }
    grep -q -- "$text" <<< "$out" || { echo "expected the message to contain '$text'"; exit 1; }
}
expect 78 xdotool -- env MALRUNAR_SESSION_KIT_WORKSPACES=a,b "$kit" workspaces --once
expect 78 xdotool -- "$kit" check
expect 78 work-browser -- "$kit" open-tabs /tmp
expect 78 work-browser -- "$kit" launchers
expect 78 'no profile to read' -- "$kit" restore-tabs
expect 78 MALRUNAR_SESSION_KIT_USER -- env -u MALRUNAR_SESSION_KIT_USER "$kit" launchers
# privileges are dropped to the named user
MALRUNAR_SESSION_KIT_BROWSER_COMMAND=/usr/bin/my-browser "$kit" launchers
test "$(stat -c %U /home/tester/.local/share/applications/malrunar-session-kit-browser.desktop)" = tester
grep -q '^WebBrowser=malrunar-session-kit-browser$' /home/tester/.config/xfce4/helpers.rc
# restore-tabs works with an explicit profile and no work-browser
python3 - <<'PY'
import ctypes, json, pathlib
raw = json.dumps({"windows": [{"tabs": [{"index": 1, "entries": [{"url": "https://one.example/"}]},
                                         {"index": 1, "entries": [{"url": "about:blank"}]},
                                         {"index": 1, "entries": [{"url": "http://two.example/"}]}]}]}).encode()
lz4 = ctypes.CDLL("liblz4.so.1")
buffer = ctypes.create_string_buffer(len(raw) + 1024)
size = lz4.LZ4_compress_default(raw, buffer, len(raw), len(buffer))
profile = pathlib.Path("/tmp/profile")
profile.mkdir(exist_ok=True)
(profile / "sessionstore.jsonlz4").write_bytes(b"mozLz40\0" + len(raw).to_bytes(4, "little") + buffer.raw[:size])
PY
out="$("$kit" restore-tabs /tmp/profile | tr '\0' ' ')"
[ "$out" = 'https://one.example/ http://two.example/ ' ] || { echo "restore-tabs printed: $out"; exit 1; }
echo gating ok
GATING
chmod 755 "$work/gating.sh"
run -v "$work:/work:ro" --user 0:0 "$consumer" /work/gating.sh || fail 'dependency gating in desktop-base'

echo '== the supervisord program: idle without settings, FATAL with a message when its tool is missing'
cat > "$work/supervised.sh" <<'SUPERVISED'
#!/usr/bin/env bash
set -euo pipefail
test -s /etc/supervisor/conf.d/malrunar-session-kit.conf
grep -q '^files = /etc/supervisor/conf.d/\*.conf' /etc/supervisor/supervisord.conf
conf=/etc/supervisor/supervisord.conf
supervisord -n -c "$conf" >> /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c "$conf" shutdown >/dev/null 2>&1 || true; sleep 1; cat /tmp/supervisord.out' EXIT
state=''
for _ in $(seq 1 30); do
    state="$(supervisorctl -c "$conf" status session-kit 2>&1 || true)"
    case "$state" in *RUNNING*|*FATAL*) break ;; esac
    sleep 1
done
sleep 5
state="$(supervisorctl -c "$conf" status session-kit 2>&1 || true)"
echo "$state"
case "$state" in *RUNNING*) ;; *) echo 'unconfigured, the program should be RUNNING (idle)'; exit 1 ;; esac
supervisorctl -c "$conf" stop session-kit
SUPERVISED
chmod 755 "$work/supervised.sh"
run -v "$work:/work:ro" "$consumer" /work/supervised.sh || fail 'supervised, unconfigured'
cat > "$work/supervised-fatal.sh" <<'SUPERVISED'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_SESSION_KIT_USER=tester MALRUNAR_SESSION_KIT_WORKSPACES=a,b
conf=/etc/supervisor/supervisord.conf
supervisord -n -c "$conf" >> /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c "$conf" shutdown >/dev/null 2>&1 || true; sleep 1; cat /tmp/supervisord.out' EXIT
state=''
for _ in $(seq 1 60); do
    state="$(supervisorctl -c "$conf" status session-kit 2>&1 || true)"
    case "$state" in *FATAL*) break ;; esac
    sleep 1
done
echo "$state"
case "$state" in *FATAL*) ;; *) echo 'a program that cannot work (no xdotool) should be FATAL, not restart forever'; exit 1 ;; esac
sleep 1
grep -q "'xdotool' is missing" /tmp/supervisord.out || { echo 'the message does not name xdotool'; exit 1; }
SUPERVISED
chmod 755 "$work/supervised-fatal.sh"
run -v "$work:/work:ro" "$consumer" /work/supervised-fatal.sh || fail 'supervised, misconfigured'

echo '== with xdotool and a virtual X server: workspaces, window placement and the session check, for real'
docker build --quiet --tag "$withx" --build-arg "CONSUMER=$consumer" --build-arg "APT_SNAPSHOT=$apt_snapshot" \
    --file "$here/consumer/x.Dockerfile" "$here/consumer" >/dev/null
cat > "$work/x.sh" <<'XTEST'
#!/usr/bin/env bash
set -euo pipefail
kit=/opt/malrunar/session-kit/bin/malrunar-session
export DISPLAY=:77
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > /tmp/xvfb.out 2>&1 &
cat > /tmp/session.sh <<'SESSION'
#!/usr/bin/env bash
set -euo pipefail
kit=/opt/malrunar/session-kit/bin/malrunar-session
xfwm4 --compositor=off > /tmp/xfwm4.out 2>&1 &
for _ in $(seq 1 60); do xdotool get_desktop >/dev/null 2>&1 && break; sleep 1; done
xdotool get_desktop >/dev/null || { echo 'no window manager'; cat /tmp/xfwm4.out; exit 1; }
python3 /work/xwindow.py Firefox Navigator browser web > /tmp/w1.out 2>&1 &
python3 /work/xwindow.py Other other main other > /tmp/w2.out 2>&1 &
for _ in $(seq 1 30); do
    [ "$(xdotool search --classname Navigator | wc -l)" = 1 ] && [ "$(xdotool search --classname other | wc -l)" = 1 ] && break
    sleep 1
done
export MALRUNAR_SESSION_KIT_WORKSPACES="Work,Chat,Scratch" MALRUNAR_SESSION_KIT_PLACE="classname=Navigator,role=browser@Scratch"
"$kit" workspaces --once
test "$(xfconf-query -c xfwm4 -p /general/workspace_count)" = 3
xfconf-query -c xfwm4 -p /general/workspace_names | grep -qx Chat
test "$(xdotool get_num_desktops)" = 3
browser="$(xdotool search --classname Navigator)"
other="$(xdotool search --classname other)"
test "$(xdotool get_desktop_for_window "$browser")" = 2 || { echo "the browser window is not on workspace 3"; exit 1; }
test "$(xdotool get_desktop_for_window "$other")" != 2 || { echo "a window that matches no rule was moved"; exit 1; }
MALRUNAR_SESSION_KIT_CHECK_WINDOWS="classname=Navigator,role=browser,count=1;classname=other,count=1" "$kit" check
if MALRUNAR_SESSION_KIT_CHECK_WINDOWS="classname=Navigator,count=2" "$kit" check; then echo 'the check passed with the wrong window count'; exit 1; fi
if MALRUNAR_SESSION_KIT_WORKSPACES="Work,Chat" "$kit" check; then echo 'the check passed with the wrong workspaces'; exit 1; fi
status=0
MALRUNAR_SESSION_KIT_CHECK_WORK_BROWSER=1 "$kit" check 2> /tmp/e || status=$?
cat /tmp/e
[ "$status" = 78 ] && grep -q 'work-browser component' /tmp/e || { echo 'the work-browser check without the component should exit 78 and name it'; exit 1; }
echo 'workspaces, placement and check ok'
SESSION
chmod +x /tmp/session.sh
dbus-run-session -- /tmp/session.sh || { cat /tmp/xvfb.out; exit 1; }
XTEST
chmod 755 "$work/x.sh"
cp "$here/xwindow.py" "$work/xwindow.py"
chmod 644 "$work/xwindow.py"
timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/home/tester -v "$work:/work:ro" "$withx" /work/x.sh \
    || fail 'workspaces, window placement or check on a virtual X server'

echo '== with the optional work-browser component present: the gated tools work against it'
docker build --quiet --tag "$withwb" --build-arg "CONSUMER=$withx" --build-arg "WORK_BROWSER_IMAGE=$work_browser" \
    --file "$here/consumer/work-browser.Dockerfile" "$here/consumer" >/dev/null
cat > "$work/wb.sh" <<'WBTEST'
#!/usr/bin/env bash
set -euo pipefail
kit=/opt/malrunar/session-kit/bin/malrunar-session
export DISPLAY=:77 MALRUNAR_WORK_BROWSER_DBUS=none
export MALRUNAR_WORK_BROWSER_FIREFOX=/tmp/firefox-stub MALRUNAR_SESSION_KIT_CHECK_BROWSER_WINDOWS=any
profile="$HOME/.config/malrunar-work-browser/profile"
mkdir -p "$profile"
# a stand-in for Firefox: logs its arguments, takes the profile lock like Firefox does, stays up;
# a handoff (-new-tab) returns at once
cat > /tmp/firefox-stub <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> /tmp/stub.log
case " $* " in *" -new-tab "*) exit 0 ;; esac
ln -sfn "127.0.0.1:+$$" "$HOME/.config/malrunar-work-browser/profile/lock"
sleep 120 &
wait
STUB
chmod +x /tmp/firefox-stub
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > /tmp/xvfb.out 2>&1 &
for _ in $(seq 1 30); do xdpyinfo -display :77 >/dev/null 2>&1 && break; sleep 1; done
python3 - <<'PY'
import ctypes, json, pathlib, os
raw = json.dumps({"windows": [{"tabs": [{"index": 1, "entries": [{"url": "https://one.example/"}]},
                                         {"index": 1, "entries": [{"url": "https://two.example/"}]}]}]}).encode()
lz4 = ctypes.CDLL("liblz4.so.1")
buffer = ctypes.create_string_buffer(len(raw) + 1024)
size = lz4.LZ4_compress_default(raw, buffer, len(raw), len(buffer))
profile = pathlib.Path(os.environ["HOME"]) / ".config/malrunar-work-browser/profile"
(profile / "sessionstore.jsonlz4").write_bytes(b"mozLz40\0" + len(raw).to_bytes(4, "little") + buffer.raw[:size])
PY
# not running yet: the helper's status 3 is passed on
status=0; "$kit" open-tabs || status=$?
[ "$status" = 3 ] || { echo "open-tabs with no browser running should exit 3, it exited $status"; exit 1; }
"$MALRUNAR_WORK_BROWSER_FIREFOX" -profile "$profile" &
for _ in $(seq 1 30); do [ -L "$profile/lock" ] && break; sleep 0.5; done
rm -f /tmp/stub.log
"$kit" open-tabs    # the default profile is the work browser's own
test "$(grep -c -- '-new-tab https://' /tmp/stub.log)" = 2
grep -q -- "-profile $profile -new-tab https://one.example/" /tmp/stub.log
grep -q -- "-profile $profile -new-tab https://two.example/" /tmp/stub.log
MALRUNAR_SESSION_KIT_CHECK_WORK_BROWSER=1 "$kit" check
"$kit" launchers
grep -q '^Exec=/opt/malrunar/work-browser/bin/malrunar-work-browser-open %u$' "$HOME/.local/share/applications/malrunar-session-kit-browser.desktop"
echo 'work-browser gated tools ok'
WBTEST
chmod 755 "$work/wb.sh"
timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/home/tester -v "$work:/work:ro" "$withwb" /work/wb.sh \
    || fail 'the tools that use the work-browser component'
echo 'session-kit smoke passed'
