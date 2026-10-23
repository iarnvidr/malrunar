#!/usr/bin/env bash
# Smoke test for the msteams-mcp component: scripts/smoke.sh msteams-mcp [image-ref]
# The component is FROM scratch, so it is checked from its file list and then exercised the way a
# consumer uses it: copied into a desktop built on desktop-base together with the node and work-browser
# components (test/consumer). It enforces the declared dependencies: with node, desktop-base or
# work-browser missing the command fails with status 78 and a message naming it. It runs the unit tests
# (the patched package against a scripted Microsoft), the real Playwright Firefox through the patched
# package, the MCP server on stdio, `sync-browser` against a real Firefox profile, and, with a virtual
# X server, the work browser and the visual browser MCP together.
#
# Needs images of desktop-base, node and work-browser: $DESKTOP_BASE_REF, $NODE_REF, $WORK_BROWSER_REF,
# else the local tags scripts/build.sh created.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "msteams-mcp smoke failed: $*" >&2; exit 1; }
tool="$root/scripts/malrunar_image.py"
local_ref() { printf 'malrunar/%s:h-%s' "$1" "$(python3 "$tool" hash "$1")"; }
desktop_base="${DESKTOP_BASE_REF:-$(local_ref desktop-base)}"
node_image="${NODE_REF:-$(local_ref node)}"
work_browser="${WORK_BROWSER_REF:-$(local_ref work-browser)}"
for ref in "$desktop_base" "$node_image" "$work_browser"; do
    docker image inspect "$ref" >/dev/null 2>&1 || fail "image $ref is not available locally (build it with scripts/build.sh <image> --load, or set DESKTOP_BASE_REF / NODE_REF / WORK_BROWSER_REF)"
done
ubuntu_image="$(python3 "$tool" pins --all msteams-mcp | sed -n 's/^UBUNTU_IMAGE=//p')"
apt_snapshot="$(python3 "$tool" pins --all msteams-mcp | sed -n 's/^APT_SNAPSHOT=//p')"
[ -n "$ubuntu_image" ] && [ -n "$apt_snapshot" ] || fail 'UBUNTU_IMAGE or APT_SNAPSHOT pin missing'

work="$(mktemp -d)"
cid=""
suffix="$$"
variants=()
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    # shellcheck disable=SC2046
    [ "${#variants[@]}" -eq 0 ] || docker rmi -f "${variants[@]}" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
chmod 755 "$work"

# variant <name> <base> <node image|scratch> <work-browser image|scratch>: builds a test consumer.
variant() {
    local name="malrunar-test/msteams-mcp-$1:$suffix"
    docker build --quiet --tag "$name" \
        --build-arg "BASE=$2" --build-arg "NODE_IMAGE=$3" --build-arg "WORK_BROWSER_IMAGE=$4" \
        --build-arg "MSTEAMS_MCP_IMAGE=$image" "$here/consumer" >/dev/null
    variants+=("$name")
    printf '%s' "$name"
}
# bounded: every container run is under timeout
run() { timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/home/tester "$@"; }

echo '== files: only /opt/malrunar/msteams-mcp (no supervisord program, no entrypoint, no other component)'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/msteams-mcp/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/msteams-mcp\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^etc/$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/msteams-mcp: $outside"
if grep -q 'supervisor' "$work/files.txt"; then fail 'the component ships a supervisord fragment; it has no long-lived program (the browser is work-browser'"'"'s)'; fi
for f in bin/malrunar-msteams-mcp lib/common.sh libexec/work-browser-mcp.mjs libexec/browser-login.mjs libexec/sync-browser-session.mjs libexec/cookie-mirror.mjs libexec/device-login.mjs \
         README.md LICENSE SOURCE.txt THIRD-PARTY.txt licenses/msteams-mcp-LICENSE licenses/MPL-2.0 licenses/playwright-LICENSE \
         app/package.json app/package-lock.json app/dist/index.js app/dist/cli.js app/node_modules/playwright/package.json; do
    test -s "$work/out/$f" || fail "$f is missing"
done
test -x "$work/out/bin/malrunar-msteams-mcp" || fail 'bin/malrunar-msteams-mcp is not executable'
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
grep -q 'MIT License' "$work/out/licenses/msteams-mcp-LICENSE" || fail 'the upstream licence is not the MIT text'
grep -q 'a69069cd3f4852122de2db0beb1a58c51876fccd' "$work/out/SOURCE.txt" || fail 'SOURCE.txt does not name the pinned commit'
ls "$work"/out/browsers/firefox-*/firefox/firefox >/dev/null || fail "Playwright's Firefox is missing"
test -e "$work"/out/browsers/firefox-*/INSTALLATION_COMPLETE || fail "Playwright's Firefox has no installation marker"
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-msteams-mcp"; bash -n "$work/out/lib/common.sh"

echo '== the patches are in the shipped package (the build also fails when one does not apply)'
for needle in "import { firefox } from 'playwright';" 'MSTEAMS_LOGIN_TIMEOUT_MS' 'async function primeSubstrateToken(page)' 'newestTeamsCookie(' 'TEAMS_WEB_SPA_CLIENT_ID' 'patched: no unexpired token'; do
    grep -rqF -- "$needle" "$work/out/app/dist" || fail "'$needle' is not in the shipped dist/"
done
grep -rq 'chromium' "$work/out/app/dist/browser/context.js" && fail 'dist/browser/context.js still mentions chromium' || true
! grep -rlq 'SSEServerTransport\|StreamableHTTPServerTransport' "$work/out/app/dist" || fail 'an HTTP transport is in the package'
test "$(find "$work/out/app/node_modules" -maxdepth 1 -name 'typescript' | wc -l)" = 0 || fail 'a development dependency (typescript) is in the package'

echo '== dependencies: each one missing is reported with status 78 and its name'
expect_missing() {   # <consumer> <word that the message must contain> <command...>
    local consumer="$1" word="$2"; shift 2
    local status=0 out
    out="$(run "$consumer" "$@" 2>&1)" || status=$?
    echo "$out"
    [ "$status" = 78 ] || fail "'$*' without $word should exit 78, it exited $status"
    grep -qi -- "$word" <<< "$out" || fail "the message does not name $word"
}
bare="$(variant bare "$ubuntu_image" scratch scratch)"
expect_missing "$bare" 'node' /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check
expect_missing "$bare" 'node' /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp serve
nodeonly="$(variant nodeonly "$ubuntu_image" "$node_image" scratch)"
expect_missing "$nodeonly" 'desktop-base' /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check
nowb="$(variant nowb "$desktop_base" "$node_image" scratch)"
expect_missing "$nowb" 'work-browser' /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check
nonode="$(variant nonode "$desktop_base" scratch "$work_browser")"
expect_missing "$nonode" 'node' /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check

echo '== the full composition: desktop-base + node + work-browser + msteams-mcp (nothing else)'
full="$(variant full "$desktop_base" "$node_image" "$work_browser")"
run "$full" bash -c '! command -v xdotool >/dev/null && ! command -v Xvfb >/dev/null && ! command -v convert >/dev/null' || fail 'extra tools are in the full consumer'
run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check | tee "$work/check.txt"
grep -q 'playwright firefox: /opt/malrunar/msteams-mcp/browsers/firefox-' "$work/check.txt" || fail 'check does not name the Playwright Firefox'
if grep -q 'missing libraries' "$work/check.txt"; then fail 'the Playwright Firefox lacks libraries on desktop-base'; fi

echo '== started as root: the user must be named (the token cache key contains the user name)'
status=0
out="$(timeout -k 10 300 docker run --rm --entrypoint '' "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check 2>&1)" || status=$?
[ "$status" = 78 ] || fail "as root without a user the command should exit 78, it exited $status"
grep -q 'MALRUNAR_MSTEAMS_MCP_USER' <<< "$out" || fail 'the message does not name MALRUNAR_MSTEAMS_MCP_USER'
out="$(timeout -k 10 300 docker run --rm --entrypoint '' -e MALRUNAR_WORK_BROWSER_USER=tester "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check 2>&1)" || fail "as root with MALRUNAR_WORK_BROWSER_USER the command failed: $out"
out="$(timeout -k 10 300 docker run --rm --entrypoint '' -e MALRUNAR_MSTEAMS_MCP_USER=nobody-here "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check 2>&1)" && fail 'a missing user was accepted' || true
grep -q "does not exist" <<< "$out" || fail 'the message for a missing user is unclear'

echo '== the opt-in and the settings that have no default are reported by name'
run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp device-login > "$work/dl.txt" 2>&1 && fail 'device-login ran without the opt-in' || [ $? = 78 ] || fail 'device-login without the opt-in should exit 78'
grep -q 'MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1' "$work/dl.txt" || fail 'the device-login message does not name the opt-in variable'
run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp login-browser > "$work/lb.txt" 2>&1 && fail 'login-browser ran without an account' || [ $? = 78 ] || fail 'login-browser without an account should exit 78'
grep -q 'MALRUNAR_MSTEAMS_MCP_UPN' "$work/lb.txt" || fail 'the login-browser message does not name MALRUNAR_MSTEAMS_MCP_UPN'
status=0; run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp login > "$work/login.txt" 2>&1 || status=$?
[ "$status" = 3 ] || fail "login without a display should exit 3, it exited $status: $(cat "$work/login.txt")"
status=0; run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp browser-mcp > "$work/bm.txt" 2>&1 || status=$?
[ "$status" = 78 ] || fail "browser-mcp without its tools should exit 78, it exited $status"
grep -q 'xdotool' "$work/bm.txt" || fail 'the browser-mcp message does not name the missing tools'
status=0; run -e MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=bogus "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp sync-browser --dry-run > "$work/bc.txt" 2>&1 || status=$?
[ "$status" = 78 ] || fail "a bad MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL should exit 78"

echo '== unit tests inside the image: cookie mirror, device-code flow, expired-token refresh, protocol (node component, as the desktop user)'
run -v "$here:/test:ro" "$full" bash -c 'cd /tmp && node --test /test/unit/*.test.mjs' || fail 'unit tests'
run -v "$here:/test:ro" "$full" /test/unit/common.test.sh /opt/malrunar/msteams-mcp || fail 'common.sh tests'

echo '== the patched package launches the real Playwright Firefox (headless, persistent profile)'
run -v "$here:/test:ro" "$full" bash -c '
    set -e
    . /opt/malrunar/msteams-mcp/lib/common.sh
    ms_find_node; ms_find_playwright_firefox; ms_export_package_env
    export LOG_LEVEL=warn
    node /test/e2e/playwright-launch.mjs
' | tee "$work/pw.txt" || fail 'the Playwright Firefox did not start through the patched package'
grep -q '"title":"patched firefox"' "$work/pw.txt" || fail 'the launched browser did not load the page'

echo '== the MCP server speaks only JSON on stdout, even when asked for info logging'
{
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
} > "$work/rpc.jsonl"
timeout -k 10 60 docker run --rm -i --entrypoint '' --user 1000:1000 -e HOME=/home/tester -e MALRUNAR_MSTEAMS_MCP_LOG_LEVEL=info "$full" \
    /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp serve < "$work/rpc.jsonl" > "$work/rpc.out" 2> "$work/rpc.err" || true
python3 -I - "$work/rpc.out" <<'PY' || fail 'the MCP server output is not clean JSON-RPC'
import json, sys
lines = [l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
messages = [json.loads(l) for l in lines]          # any non-JSON line (a log line) fails here
assert len(messages) >= 2, f"expected an initialize and a tools/list reply, got {len(messages)}"
tools = {t["name"] for m in messages if m.get("id") == 2 for t in m["result"]["tools"]}
assert {"teams_login", "teams_status", "teams_search"} <= tools, sorted(tools)
print("tools:", len(tools))
PY
status=0; run "$full" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp status > "$work/status.txt" 2>&1 || status=$?
echo "status exit $status: $(head -c 300 "$work/status.txt")"
grep -qi 'session' "$work/status.txt" || fail 'status printed nothing about the session'

echo '== sync-browser against a real Firefox profile (cookies.sqlite of the stock Firefox)'
run -v "$here:/test:ro" "$full" /test/e2e/sync-test.sh || fail 'sync-browser test'

echo '== the visual browser MCP and `open`, with a virtual display and the real work browser'
tools="malrunar-test/msteams-mcp-tools:$suffix"
docker build --quiet --tag "$tools" --build-arg "CONSUMER=$full" --build-arg "APT_SNAPSHOT=$apt_snapshot" \
    --file "$here/consumer/tools.Dockerfile" "$here/consumer" >/dev/null
variants+=("$tools")
timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/home/tester --shm-size 512m -v "$here:/test:ro" "$tools" /test/e2e/display-test.sh \
    || fail 'browser-mcp / open test'
run "$tools" /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp check | grep -q 'browser-mcp tools: present' || fail 'check does not see the tools'
echo 'msteams-mcp smoke passed'
