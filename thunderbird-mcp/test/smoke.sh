#!/usr/bin/env bash
# Smoke test for the thunderbird-mcp component: scripts/smoke.sh thunderbird-mcp [image-ref]
# The component is FROM scratch, so it is checked from its file list and then run the way a consumer uses it.
# It enforces the declared dependency (node and nothing else): in the bare pinned Ubuntu image the wrapper exits
# 78 and names node; with the node component added the bridge runs against a stand-in for the add-on (no
# desktop-base, no Thunderbird in that image). A last, separate end-to-end test uses desktop-base, the thunderbird
# and node components and a virtual display, with the add-on installed through the policy fragment the
# component ships (in a throwaway image), and drives the real add-on through the bridge.
#
# Needs images: desktop-base ($DESKTOP_BASE_REF), node ($NODE_REF) and thunderbird ($THUNDERBIRD_REF), each defaulting
# to the local tag scripts/build.sh created.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "thunderbird-mcp smoke failed: $*" >&2; exit 1; }
tool="$root/scripts/malrunar_image.py"
local_ref() { printf 'malrunar/%s:h-%s' "$1" "$(python3 "$tool" hash "$1")"; }
desktop_base="${DESKTOP_BASE_REF:-$(local_ref desktop-base)}"
node_image="${NODE_REF:-$(local_ref node)}"
thunderbird_image="${THUNDERBIRD_REF:-$(local_ref thunderbird)}"
for ref in "$desktop_base" "$node_image" "$thunderbird_image"; do
    docker image inspect "$ref" >/dev/null 2>&1 || fail "image $ref is not available locally (build it with scripts/build.sh, or set DESKTOP_BASE_REF, NODE_REF or THUNDERBIRD_REF)"
done
pins() { python3 "$tool" pins --all thunderbird-mcp | sed -n "s/^$1=//p"; }
ubuntu_image="$(pins UBUNTU_IMAGE)"; apt_snapshot="$(pins APT_SNAPSHOT)"
mcp_version="$(pins THUNDERBIRD_MCP_VERSION)"; per_image="$(pins MAX_INLINE_IMAGE_BASE64_BYTES)"; total="$(pins MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES)"
[ -n "$ubuntu_image" ] && [ -n "$apt_snapshot" ] && [ -n "$mcp_version" ] || fail 'pins missing'

work="$(mktemp -d)"
cid=""
bare="malrunar-test/thunderbird-mcp-bare:$$"
withnode="malrunar-test/thunderbird-mcp-node:$$"
e2e="malrunar-test/thunderbird-mcp-e2e:$$"
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    docker rmi -f "$e2e" "$withnode" "$bare" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
chmod 755 "$work"
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$@"; }

echo '== files: only /opt/malrunar/thunderbird-mcp, nothing for supervisord, no Thunderbird, no node'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/thunderbird-mcp/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/thunderbird-mcp\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^etc/$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/thunderbird-mcp: $outside"
for f in mcp-bridge.cjs package.json thunderbird-mcp.xpi UPSTREAM-LICENSE LICENSE SOURCE.txt README.md bin/malrunar-thunderbird-mcp \
         share/policies-extension.json.example share/mcp-client.json.example; do
    test -s "$work/out/$f" || fail "$f is missing"
done
if grep -Eiq 'supervisor|/thunderbird/|omni\.ja|(^|/)node$|node_modules' "$work/files.txt"; then fail 'the component ships a supervisord fragment, Thunderbird or node'; fi
grep -q '^MIT License' "$work/out/UPSTREAM-LICENSE" || fail 'UPSTREAM-LICENSE is not the MIT text'
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-thunderbird-mcp"
python3 -I -c 'import json,sys; [json.load(open(f)) for f in sys.argv[1:]]' "$work/out/share/policies-extension.json.example" "$work/out/share/mcp-client.json.example"

echo '== the add-on: packed from the pinned source with the pinned limits'
python3 -I - "$work/out/thunderbird-mcp.xpi" "$mcp_version" "$per_image" "$total" <<'PY'
import json, sys, zipfile
path, version, per_image, total = sys.argv[1:]
z = zipfile.ZipFile(path)
assert z.testzip() is None
m = json.loads(z.read("manifest.json"))
assert m["browser_specific_settings"]["gecko"]["id"] == "thunderbird-mcp@tkasperczyk.dev", m
assert m["version"].startswith(version + "."), m["version"]
api = z.read("mcp_server/api.js").decode()
assert f"const MAX_INLINE_IMAGE_BASE64_BYTES = {per_image};" in api
assert f"const MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES = {total};" in api
print("add-on", m["version"], "ok")
PY
grep -q "^thunderbird-mcp ${mcp_version}\$" "$work/out/SOURCE.txt" || fail 'SOURCE.txt does not name the version'

echo '== dependency: without node the wrapper fails clearly and says what is missing'
docker build --quiet --target bare --tag "$bare" \
    --build-arg "MCP_IMAGE=$image" --build-arg "NODE_IMAGE=$node_image" --build-arg "BASE=$ubuntu_image" "$here/consumer" >/dev/null
status=0
out="$(run --user 1000:1000 "$bare" /opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp --check 2>&1)" || status=$?
echo "$out"
[ "$status" = 78 ] || fail "without node the wrapper should exit 78, it exited $status"
grep -q 'node' <<< "$out" || fail 'the message without node does not name node'

echo '== bare Ubuntu + node component + this component (no desktop-base, no Thunderbird)'
docker build --quiet --target with-node --tag "$withnode" \
    --build-arg "MCP_IMAGE=$image" --build-arg "NODE_IMAGE=$node_image" --build-arg "BASE=$ubuntu_image" "$here/consumer" >/dev/null
run --user 1000:1000 "$withnode" bash -c '! test -e /opt/malrunar/thunderbird && ! test -e /opt/malrunar/firefox' \
    || fail 'the test image has more than the pinned Ubuntu image, node and the component'
cp "$here/bridge-unit.cjs" "$here/bridge-roundtrip.cjs" "$work/"
run --user 1000:1000 -e HOME=/tmp -e PATH=/opt/malrunar/node/bin:/usr/bin:/bin -v "$work:/work:ro" "$withnode" bash -c '
    set -e
    /opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp --check
    node /work/bridge-unit.cjs /opt/malrunar/thunderbird-mcp/mcp-bridge.cjs
    node /work/bridge-roundtrip.cjs /opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp
' || fail 'the bridge does not work with the node component'

echo '== end to end: the real add-on in a running Thunderbird, driven through the bridge'
docker build --quiet --tag "$e2e" \
    --build-arg "BASE=$desktop_base" --build-arg "THUNDERBIRD_IMAGE=$thunderbird_image" --build-arg "NODE_IMAGE=$node_image" \
    --build-arg "MCP_IMAGE=$image" --build-arg "APT_SNAPSHOT=$apt_snapshot" -f "$here/consumer/e2e.Dockerfile" "$here/consumer" >/dev/null
cp "$here/bridge-e2e.cjs" "$work/"
cat > "$work/e2e.sh" <<'E2E'
#!/usr/bin/env bash
set -euo pipefail
export PATH=/opt/malrunar/node/bin:$PATH
export MALRUNAR_THUNDERBIRD_USER=tester MALRUNAR_THUNDERBIRD_RETRY_SECONDS=2
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > /tmp/xvfb.out 2>&1 &
supervisord -n -c /etc/supervisor/supervisord.conf > /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; tail -n 60 /tmp/supervisord.out' EXIT
for _ in $(seq 1 120); do
    [ -s /tmp/thunderbird-mcp/connection.json ] && break
    sleep 1
done
[ -s /tmp/thunderbird-mcp/connection.json ] || { ls -la /tmp /tmp/thunderbird-mcp 2>&1 | head -30; echo 'the add-on never wrote its connection file'; exit 1; }
# the add-on runs as the desktop user: so does the bridge
setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/tester PATH="$PATH" \
    node /work/bridge-e2e.cjs /opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp
E2E
chmod 755 "$work/e2e.sh"
run --shm-size 512m -v "$work:/work:ro" "$e2e" /work/e2e.sh || fail 'end-to-end test'

echo 'thunderbird-mcp smoke passed'
