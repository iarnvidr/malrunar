#!/usr/bin/env bash
# Smoke test for the node component: scripts/smoke.sh node [image-ref]
# The component is FROM scratch, so it is checked from its file list and then run the way a consumer
# uses it: copied into the bare pinned Ubuntu image (test/consumer), with no desktop-base and no
# other component, which is how its "depends on nothing" claim is enforced.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "node smoke failed: $*" >&2; exit 1; }
pin() { python3 "$root/scripts/malrunar_image.py" pins --all node | sed -n "s/^$1=//p"; }
node_version="$(pin NODE_VERSION)"
ubuntu_image="$(pin UBUNTU_IMAGE)"
[ -n "$node_version" ] && [ -n "$ubuntu_image" ] || fail 'pins missing'

work="$(mktemp -d)"
cid=""
consumer="malrunar-test/node-consumer:$$"
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    docker rmi -f "$consumer" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

echo '== files: only /opt/malrunar/node, no entrypoint, nothing for supervisord'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/node\(/\|$\)' "$work/files.txt" | grep -v '^opt/$\|^opt/malrunar/$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^etc/$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/node: $outside"
for f in bin/node bin/npm bin/npx LICENSE README.md SOURCE.txt lib/node_modules/npm/package.json; do
    grep -qx "opt/malrunar/node/$f" "$work/files.txt" || fail "opt/malrunar/node/$f is missing"
done
if grep -q 'supervisor' "$work/files.txt"; then fail 'the component ships a supervisord fragment; node is a library-like runtime and starts nothing'; fi
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" \
    || fail 'component image sets an entrypoint'

echo '== consumer: the bare Ubuntu image plus the component'
docker build --quiet --tag "$consumer" \
    --build-arg "NODE_IMAGE=$image" --build-arg "UBUNTU_IMAGE=$ubuntu_image" \
    "$here/consumer" >/dev/null
run() { timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/tmp "$consumer" "$@"; }

echo '== node and npm report the pinned version'
out="$(run node --version)"
echo "node $out"
test "$out" = "v${node_version}" || fail "node reports '$out', pinned v${node_version}"
out="$(run npm --version)"
echo "npm $out"
test -n "$out" || fail 'npm printed nothing'
run npx --version >/dev/null || fail 'npx does not run'

echo '== node runs a script: crypto, zlib, a loopback HTTP round trip and fetch'
run node -e '
const http = require("node:http"), zlib = require("node:zlib"), crypto = require("node:crypto");
const hash = crypto.createHash("sha256").update("malrunar").digest("hex");
if (hash.length !== 64) throw new Error("sha256");
if (zlib.gunzipSync(zlib.gzipSync("ok")).toString() !== "ok") throw new Error("zlib");
const server = http.createServer((req, res) => res.end("pong")).listen(0, "127.0.0.1", async () => {
  const body = await (await fetch(`http://127.0.0.1:${server.address().port}/`)).text();
  server.close();
  if (body !== "pong") throw new Error("http round trip: " + body);
  console.log("node script ok", process.version, process.arch);
});
' || fail 'node script'

echo '== npm can run a package script (no network needed)'
run bash -c '
    set -e
    d="$(mktemp -d)"; cd "$d"
    printf "{\"name\":\"t\",\"version\":\"1.0.0\",\"scripts\":{\"hello\":\"node -p 6*7\"}}" > package.json
    test "$(npm run --silent hello)" = 42
' || fail 'npm run'

echo '== no ambient dependency: node still runs with an empty PATH and a scrubbed environment'
run env -i /opt/malrunar/node/bin/node -p 'process.versions.node' | grep -qx "${node_version}" || fail 'node needs something from the environment'
echo 'node smoke passed'
