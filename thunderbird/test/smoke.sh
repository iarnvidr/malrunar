#!/usr/bin/env bash
# Smoke test for the thunderbird component: scripts/smoke.sh thunderbird [image-ref]
# The component is FROM scratch, so it is checked from its file list and then run the way a consumer uses
# it: copied into a desktop built on desktop-base (test/consumer). It enforces the declared dependency
# (desktop-base only: without it the launcher exits 78 and names it; the test image has no node and no
# other component), that Thunderbird is exactly the unmodified Mozilla release (checksums recorded at
# build time), that it starts under supervisord, and that the consumer-side changes
# (consumer/customize.py: omni.ja patch, add-ons, policies) apply to a COPY of it in a throwaway image
# that is never published, leave only omni.ja and distribution/ different, and work.
#
# Needs a desktop-base image ($DESKTOP_BASE_REF, else the local tag scripts/build.sh created) and a node
# component image for the consumer-side test only ($NODE_REF, else its local tag).
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "thunderbird smoke failed: $*" >&2; exit 1; }
tool="$root/scripts/malrunar_image.py"
desktop_base="${DESKTOP_BASE_REF:-malrunar/desktop-base:h-$(python3 "$tool" hash desktop-base)}"
node_image="${NODE_REF:-malrunar/node:h-$(python3 "$tool" hash node)}"
docker image inspect "$desktop_base" >/dev/null 2>&1 || fail "desktop-base image $desktop_base is not available locally (build it with scripts/build.sh desktop-base --load, or set DESKTOP_BASE_REF)"
docker image inspect "$node_image" >/dev/null 2>&1 || fail "node image $node_image is not available locally (build it with scripts/build.sh node --load, or set NODE_REF)"
pins() { python3 "$tool" pins --all thunderbird | sed -n "s/^$1=//p"; }
ubuntu_image="$(pins UBUNTU_IMAGE)"; apt_snapshot="$(pins APT_SNAPSHOT)"; version="$(pins THUNDERBIRD_VERSION)"
[ -n "$ubuntu_image" ] && [ -n "$apt_snapshot" ] && [ -n "$version" ] || fail 'pins missing'

work="$(mktemp -d)"
cid=""
bare="malrunar-test/thunderbird-bare:$$"
consumer="malrunar-test/thunderbird-consumer:$$"
withx="malrunar-test/thunderbird-x:$$"
custom="malrunar-test/thunderbird-customized:$$"
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    docker rmi -f "$custom" "$withx" "$consumer" "$bare" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
chmod 755 "$work"   # the desktop user (uid 1000) reads the mounted files
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$@"; }

echo '== files: the component and its supervisord fragment only'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/thunderbird\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^etc/$\|^etc/supervisor/$\|^etc/supervisor/conf.d/$\|^etc/supervisor/conf.d/malrunar-thunderbird.conf$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/thunderbird and the supervisord fragment: $outside"
for f in app/thunderbird app/omni.ja app/application.ini unmodified.sha256 SOURCE.txt LICENSE LICENSE-MPL-2.0 README.md \
         bin/malrunar-thunderbird lib/common.sh consumer/customize.py consumer/oauth_shared_mailbox.py consumer/addons.json \
         consumer/policies/maildir-and-oauth.json consumer/policies/tbsync.json; do
    grep -qx "opt/malrunar/thunderbird/$f" "$work/files.txt" || fail "opt/malrunar/thunderbird/$f is missing"
done
if grep -Ei 'node_modules|(^|/)node$|firefox|\.xpi$|(^|/)policies\.json$|MODIFIED\.txt' "$work/files.txt"; then
    fail 'the component ships node, Firefox, an add-on, a policy file or a modification marker'
fi
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" || fail 'component image sets an entrypoint'

echo '== dependency: without desktop-base the launcher fails clearly and says what is missing'
docker build --quiet --tag "$bare" \
    --build-arg "THUNDERBIRD_IMAGE=$image" --build-arg "BASE=$ubuntu_image" "$here/consumer" >/dev/null
status=0
out="$(run --user 1000:1000 -e HOME=/tmp -e MALRUNAR_THUNDERBIRD_DISPLAY=none "$bare" \
    /opt/malrunar/thunderbird/bin/malrunar-thunderbird 2>&1)" || status=$?
echo "$out"
[ "$status" = 78 ] || fail "without desktop-base the launcher should exit 78, it exited $status"
grep -q 'desktop-base' <<< "$out" || fail 'the message without desktop-base does not name desktop-base'
status=0
out="$(run -e MALRUNAR_THUNDERBIRD_DISPLAY=none "$bare" /opt/malrunar/thunderbird/bin/malrunar-thunderbird 2>&1)" || status=$?
[ "$status" = 78 ] && grep -q MALRUNAR_THUNDERBIRD_USER <<< "$out" || fail "as root without a user: exit $status: $out"

echo '== consumer image: desktop-base + the component, and nothing else'
docker build --quiet --tag "$consumer" \
    --build-arg "THUNDERBIRD_IMAGE=$image" --build-arg "BASE=$desktop_base" "$here/consumer" >/dev/null
run "$consumer" bash -c '! command -v node >/dev/null && ! command -v npm >/dev/null && ! test -e /opt/malrunar/node && ! test -e /opt/malrunar/work-browser && ! test -e /opt/malrunar/session-kit && ! test -e /opt/malrunar/thunderbird-mcp' \
    || fail 'node or another component is present in the test consumer; the component must not need them'

echo '== Thunderbird is exactly the unmodified Mozilla release, and starts'
run "$consumer" bash -c '
    set -e
    cd /opt/malrunar/thunderbird/app && LC_ALL=C sha256sum -c --quiet ../unmodified.sha256
    test ! -e distribution && test ! -e ../MODIFIED.txt && test "$(find /opt/malrunar/thunderbird/app ! -user root | wc -l)" = 0
    test -z "$(find / -xdev -name policies.json -path "*hunderbird*" 2>/dev/null)"
' || fail 'the Thunderbird installation differs from the release, or a policy file exists'
out="$(run "$consumer" /opt/malrunar/thunderbird/app/thunderbird --version)"
echo "$out"
grep -q "Thunderbird ${version}\$" <<< "$out" || fail "thunderbird --version printed '$out', pinned ${version}"
run "$consumer" bash -c '
    export LD_LIBRARY_PATH=/opt/malrunar/thunderbird/app   # Thunderbird brings its own NSS, sqlite and friends
    missing="$(ldd /opt/malrunar/thunderbird/app/libxul.so /opt/malrunar/thunderbird/app/thunderbird-bin | grep "not found" || true)"
    [ -z "$missing" ] || { echo "$missing"; exit 1; }
' || fail 'Thunderbird links against libraries that desktop-base does not provide'
bash -n "$here/../bin/malrunar-thunderbird"; bash -n "$here/../lib/common.sh"

echo '== supervised, headless: the launcher starts Thunderbird, the lock is taken, a missing user is reported'
cat > "$work/headless.sh" <<'HEADLESS'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_THUNDERBIRD_USER=tester MALRUNAR_THUNDERBIRD_DISPLAY=none MALRUNAR_THUNDERBIRD_RETRY_SECONDS=2
profile=/home/tester/.config/malrunar-thunderbird/profile
supervisord -n -c /etc/supervisor/supervisord.conf > /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; cat /tmp/supervisord.out' EXIT
for _ in $(seq 1 90); do
    [ -L "$profile/lock" ] && break
    sleep 1
done
[ -L "$profile/lock" ] || { echo 'Thunderbird never took the profile lock'; exit 1; }
owner="$(readlink "$profile/lock")"; owner="${owner##*+}"
ps -o args= -p "$owner" | grep -q -- '/opt/malrunar/thunderbird/app/thunderbird'
sleep 10
for _ in $(seq 1 15); do
    supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird | grep -q RUNNING && exit 0
    sleep 1
done
supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird
exit 1
HEADLESS
chmod 755 "$work/headless.sh"
run --shm-size 512m -v "$work:/work:ro" "$consumer" /work/headless.sh || fail 'supervised headless Thunderbird'
cat > "$work/nouser.sh" <<'NOUSER'
#!/usr/bin/env bash
set -u
supervisord -n -c /etc/supervisor/supervisord.conf > /tmp/supervisord.out 2>&1 &
for _ in $(seq 1 40); do
    supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird 2>/dev/null | grep -q FATAL && break
    sleep 1
done
supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird
grep -q 'MALRUNAR_THUNDERBIRD_USER' /tmp/supervisord.out || { cat /tmp/supervisord.out; exit 1; }
supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird | grep -q FATAL
NOUSER
chmod 755 "$work/nouser.sh"
run -v "$work:/work:ro" "$consumer" /work/nouser.sh || fail 'a missing MALRUNAR_THUNDERBIRD_USER is not reported and left to retry forever'

echo '== on a display: a Thunderbird window appears on an X server'
docker build --quiet --tag "$withx" --build-arg "CONSUMER=$consumer" --build-arg "APT_SNAPSHOT=$apt_snapshot" \
    -f "$here/consumer/xvfb.Dockerfile" "$here/consumer" >/dev/null
cat > "$work/display.sh" <<'DISPLAYTEST'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_THUNDERBIRD_USER=tester MALRUNAR_THUNDERBIRD_RETRY_SECONDS=2
profile=/home/tester/.config/malrunar-thunderbird/profile
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > /tmp/xvfb.out 2>&1 &
supervisord -n -c /etc/supervisor/supervisord.conf > /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; cat /tmp/xvfb.out /tmp/supervisord.out' EXIT
for _ in $(seq 1 90); do
    [ -L "$profile/lock" ] && break
    sleep 1
done
[ -L "$profile/lock" ] || { echo 'Thunderbird never took the profile lock'; exit 1; }
for _ in $(seq 1 90); do
    xwininfo -display :77 -root -tree 2>/dev/null | grep -qi 'thunderbird' && break
    sleep 1
done
xwininfo -display :77 -root -tree | grep -qi 'thunderbird' || { xwininfo -display :77 -root -tree; echo 'no Thunderbird window on the display'; exit 1; }
grep -q 'display :77' /tmp/supervisord.out || { echo 'the launcher did not report the display it found'; exit 1; }
supervisorctl -c /etc/supervisor/supervisord.conf status thunderbird | grep -q RUNNING
DISPLAYTEST
chmod 755 "$work/display.sh"
run --shm-size 512m -v "$work:/work:ro" "$withx" /work/display.sh || fail 'supervised Thunderbird on a display'

echo '== consumer side: customize.py on a copy, in a throwaway image that is never published'
docker build --quiet --tag "$custom" --build-arg "CONSUMER=$consumer" --build-arg "NODE_IMAGE=$node_image" \
    -f "$here/consumer/customized.Dockerfile" "$here/consumer" >/dev/null
echo '-- only omni.ja, distribution/policies.json and the marker differ from the release'
run "$custom" bash -c '
    cd /opt/malrunar/thunderbird/app
    changed="$(LC_ALL=C sha256sum -c ../unmodified.sha256 2>&1 | grep -v ": OK$" | grep -v "^sha256sum: WARNING" || true)"
    echo "$changed"
    [ "$changed" = "./omni.ja: FAILED" ] || exit 1
    test -f distribution/policies.json && test -f ../MODIFIED.txt
    test "$(find distribution -type f | wc -l)" = 1
    cat ../MODIFIED.txt
' || fail 'the customized tree differs from the release in more than omni.ja and distribution/policies.json'
echo '-- the OAuth module test passes on the patched module and fails on the unmodified one'
cp "$here/oauth-shared-mailbox.test.mjs" "$work/"
cat > "$work/oauth.sh" <<'OAUTH'
#!/usr/bin/env bash
set -euo pipefail
export PATH=/opt/malrunar/node/bin:$PATH
extract() { python3 -I -c 'import sys, zipfile; open(sys.argv[2], "wb").write(zipfile.ZipFile(sys.argv[1]).read("modules/OAuth2Module.sys.mjs"))' "$1" "$2"; }
extract /opt/malrunar/thunderbird/app/omni.ja /tmp/patched.sys.mjs
node /work/oauth-shared-mailbox.test.mjs /tmp/patched.sys.mjs
# the same check must fail on Mozilla's own file: it proves the test looks at the patch
if [ -f /opt/malrunar/thunderbird-pristine/omni.ja ]; then
    extract /opt/malrunar/thunderbird-pristine/omni.ja /tmp/original.sys.mjs
    if node /work/oauth-shared-mailbox.test.mjs /tmp/original.sys.mjs >/dev/null 2>&1; then
        echo 'the OAuth test also passes on the unmodified module: it does not test the patch'; exit 1
    fi
    echo 'the test rejects the unmodified module'
fi
OAUTH
chmod 755 "$work/oauth.sh"
# the unmodified omni.ja, taken from the component image itself
cid="$(docker create "$image" /unused)"
mkdir "$work/pristine"
docker cp "$cid:/opt/malrunar/thunderbird/app/omni.ja" "$work/pristine/omni.ja"
docker rm -f "$cid" >/dev/null; cid=""
run -v "$work:/work:ro" -v "$work/pristine:/opt/malrunar/thunderbird-pristine:ro" "$custom" /work/oauth.sh || fail 'OAuth module test'
echo '-- the add-ons: manifests say they are compatible with this Thunderbird, and nothing else changed in them'
cat > "$work/addons.sh" <<'ADDONS'
#!/usr/bin/env bash
set -euo pipefail
python3 -I - <<'PY'
import json, zipfile
for name, addon_id in (("tbsync", "tbsync@jobisoft.de"), ("eas4tbsync", "eas4tbsync@jobisoft.de")):
    z = zipfile.ZipFile(f"/opt/malrunar/thunderbird-addons/{name}.xpi")
    assert z.testzip() is None
    m = json.loads(z.read("manifest.json"))
    g = (m.get("browser_specific_settings") or m["applications"])["gecko"]
    assert g["id"] == addon_id, g
    assert g["strict_max_version"].endswith(".*") and g["strict_max_version"].split(".")[0].isdigit(), g
    print(name, g["id"], "strict_max_version", g["strict_max_version"])
PY
ADDONS
chmod 755 "$work/addons.sh"
run -v "$work:/work:ro" "$custom" /work/addons.sh || fail 'add-on manifests'
echo '-- the customized Thunderbird starts headless and installs the add-ons named in its policies'
cat > "$work/custom-run.sh" <<'CUSTOMRUN'
#!/usr/bin/env bash
set -euo pipefail
profile=/home/tester/.config/malrunar-thunderbird/profile
export MALRUNAR_THUNDERBIRD_DISPLAY=none MALRUNAR_THUNDERBIRD_RETRY_SECONDS=2
/opt/malrunar/thunderbird/bin/malrunar-thunderbird > /tmp/tb.log 2>&1 &
launcher=$!
trap 'kill "$launcher" 2>/dev/null || true; pkill -u tester -f /opt/malrunar/thunderbird/app 2>/dev/null || true; sleep 2; tail -n 40 /tmp/tb.log' EXIT
for _ in $(seq 1 90); do
    [ -L "$profile/lock" ] && break
    sleep 1
done
[ -L "$profile/lock" ] || { echo 'the customized Thunderbird never took the profile lock'; exit 1; }
found=0
for _ in $(seq 1 90); do
    if [ -f "$profile/extensions.json" ] && grep -q 'tbsync@jobisoft.de' "$profile/extensions.json" && grep -q 'eas4tbsync@jobisoft.de' "$profile/extensions.json"; then found=1; break; fi
    sleep 1
done
[ "$found" = 1 ] || { ls -la "$profile" | head -40; echo 'the policy-installed add-ons are not in extensions.json'; exit 1; }
kill -0 "$launcher"
owner="$(readlink "$profile/lock")"; owner="${owner##*+}"
kill -0 "$owner"
echo 'customized Thunderbird is running with the add-ons installed'
CUSTOMRUN
chmod 755 "$work/custom-run.sh"
run --user 1000:1000 -e HOME=/home/tester --shm-size 512m -v "$work:/work:ro" "$custom" /work/custom-run.sh || fail 'customized Thunderbird'

echo 'thunderbird smoke passed'
