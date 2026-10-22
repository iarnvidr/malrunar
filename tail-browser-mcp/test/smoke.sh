#!/usr/bin/env bash
# Smoke test for the tail-browser-mcp component: scripts/smoke.sh tail-browser-mcp [image-ref]
# The image is FROM scratch, so its files are copied out and run with the digest-pinned node image
# from pins.env: the server, the client and the extension are exercised against a fake extension.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "tail-browser-mcp smoke failed: $*" >&2; exit 1; }
node_image="$(python3 "$root/scripts/malrunar_image.py" pins tail-browser-mcp | sed -n 's/^NODE_IMAGE=//p')"
browser_id="$(python3 "$root/scripts/malrunar_image.py" pins tail-browser-mcp | sed -n 's/^TAIL_BROWSER_ID=//p')"
[ -n "$node_image" ] && [ -n "$browser_id" ] || fail 'pins missing'

work="$(mktemp -d)"
cid=""
cleanup() { [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

echo '== files'
cid="$(docker create "$image" /unused)"
docker cp "$cid:/opt/malrunar/tail-browser-mcp/." "$work/"
for f in server.cjs client.mjs tail.xpi TAIL-LICENSE THIRD-PARTY-LICENSES.txt SOURCE.txt; do
    test -s "$work/$f" || fail "missing $f"
done
grep -q 'MIT License' "$work/TAIL-LICENSE" || fail 'TAIL-LICENSE is not the MIT text'
grep -q '^@modelcontextprotocol/sdk@' "$work/THIRD-PARTY-LICENSES.txt" || fail 'bundled SDK missing from the notice'
grep -q "commit bf5645d784d2de87ade1a99c583fee518537f146" "$work/SOURCE.txt" || fail 'source commit not recorded'
if grep -rIl -E '/home/[a-z]|/Users/[a-z]' "$work"; then fail 'build-host paths in the bundle'; fi

echo '== extension'
python3 - "$work/tail.xpi" "$browser_id" <<'PY'
import json, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
names = set(z.namelist())
for need in ('manifest.json', 'dist/background.js', 'dist/options.js', 'options.html'):
    assert need in names, f'xpi lacks {need}'
manifest = json.loads(z.read('manifest.json'))
assert manifest['version'] == '2.0.23.2', manifest['version']
assert 'http://127.0.0.1/*' in manifest['permissions']
assert f'config.browserId = "{sys.argv[2]}"' in z.read('dist/background.js').decode(), 'browser id not baked in'
PY

echo '== server and client round trip (pinned node image)'
docker run --rm --network none \
    -v "$work:/opt/malrunar/tail-browser-mcp:ro" \
    -v "$here/test-tail-client.mjs:/test/test-tail-client.mjs:ro" \
    -e TAIL_BROWSER_ID="$browser_id" \
    "$node_image" node --test /test/test-tail-client.mjs
echo 'tail-browser-mcp smoke passed'
