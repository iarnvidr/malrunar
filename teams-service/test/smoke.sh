#!/usr/bin/env bash
# Smoke test for the teams-service component: scripts/smoke.sh teams-service [image-ref]
# The component is FROM scratch, so it is checked from its file list and then exercised the way a
# consumer uses it: copied into the bare pinned Ubuntu image together with the node component and the
# msteams-mcp component and NOTHING else (no desktop-base, no work-browser: the dependencies this
# component declares are exactly these two), and once into a desktop-base image to see the supervisord
# program run. Missing node or msteams-mcp must be reported with status 78 and the component's name.
# It runs the ported unit tests inside the image (the node component), including those that start the
# real PocketBase, then starts the service and PocketBase and probes the API.
#
# Needs images of node, msteams-mcp and desktop-base: $NODE_REF, $MSTEAMS_MCP_REF, $DESKTOP_BASE_REF, else
# the local tags scripts/build.sh created.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
fail() { echo "teams-service smoke failed: $*" >&2; exit 1; }
tool="$root/scripts/malrunar_image.py"
local_ref() { printf 'malrunar/%s:h-%s' "$1" "$(python3 "$tool" hash "$1")"; }
node_image="${NODE_REF:-$(local_ref node)}"
msteams="${MSTEAMS_MCP_REF:-$(local_ref msteams-mcp)}"
desktop_base="${DESKTOP_BASE_REF:-$(local_ref desktop-base)}"
for ref in "$node_image" "$msteams" "$desktop_base"; do
    docker image inspect "$ref" >/dev/null 2>&1 || fail "image $ref is not available locally (build it with scripts/build.sh <image> --load, or set NODE_REF / MSTEAMS_MCP_REF / DESKTOP_BASE_REF)"
done
ubuntu_image="$(python3 "$tool" pins --all teams-service | sed -n 's/^UBUNTU_IMAGE=//p')"
pocketbase_version="$(python3 "$tool" pins teams-service | sed -n 's/^POCKETBASE_VERSION=//p')"
[ -n "$ubuntu_image" ] && [ -n "$pocketbase_version" ] || fail 'UBUNTU_IMAGE or POCKETBASE_VERSION pin missing'

work="$(mktemp -d)"
cid=""
suffix="$$"
variants=()
cleanup() {
    [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
    [ "${#variants[@]}" -eq 0 ] || docker rmi -f "${variants[@]}" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
chmod 755 "$work"

# variant <name> <base> <node image|scratch> <msteams-mcp image|scratch>
variant() {
    local name="malrunar-test/teams-service-$1:$suffix"
    docker build --quiet --tag "$name" \
        --build-arg "BASE=$2" --build-arg "NODE_IMAGE=$3" --build-arg "MSTEAMS_MCP_IMAGE=$4" \
        --build-arg "TEAMS_SERVICE_IMAGE=$image" "$here/consumer" >/dev/null
    variants+=("$name")
    printf '%s' "$name"
}
run() { timeout -k 10 300 docker run --rm --entrypoint '' --user 1000:1000 -e HOME=/home/tester "$@"; }
launcher=/opt/malrunar/teams-service/bin/malrunar-teams-service

echo '== files: /opt/malrunar/teams-service and the one supervisord fragment, nothing else'
cid="$(docker create "$image" /unused)"
docker export "$cid" | tar -t > "$work/files.txt"
mkdir "$work/out"
docker cp "$cid:/opt/malrunar/teams-service/." "$work/out/"
docker rm -f "$cid" >/dev/null; cid=""
outside="$(grep -v '^opt/malrunar/teams-service\(/\|$\)' "$work/files.txt" \
    | grep -v '^opt/$\|^opt/malrunar/$\|^etc/$\|^etc/supervisor/$\|^etc/supervisor/conf.d/$\|^etc/supervisor/conf.d/malrunar-teams-service.conf$\|^\.dockerenv$\|^dev/\|^etc/hostname$\|^etc/hosts$\|^etc/mtab$\|^etc/resolv.conf$\|^proc/\|^sys/\|^dev$\|^proc$\|^sys$' || true)"
[ -z "$outside" ] || fail "files outside /opt/malrunar/teams-service and the supervisord fragment: $outside"
if grep -Ei 'node_modules|(^|/)node$|firefox|playwright|msteams-mcp/' "$work/files.txt"; then fail 'the component ships node, a browser or the msteams-mcp package; they are separate components'; fi
for f in bin/malrunar-teams-service bin/malrunar-teams-pocketbase lib/common.sh src/main.mjs src/lib.mjs src/server.mjs dev/fixture.mjs package.json README.md LICENSE SOURCE.txt THIRD-PARTY.txt licenses/github.com_pocketbase_pocketbase/LICENSE.md; do
    test -s "$work/out/$f" || fail "$f is missing"
done
test -x "$work/out/bin/malrunar-teams-service" && test -x "$work/out/bin/malrunar-teams-pocketbase" || fail 'a program is not executable'
grep -q 'Apache License' "$work/out/LICENSE" || fail 'LICENSE is not the Apache-2.0 text'
grep -q "v${pocketbase_version}" "$work/out/THIRD-PARTY.txt" || grep -q "pocketbase/pocketbase@v${pocketbase_version}" "$work/out/THIRD-PARTY.txt" || fail "THIRD-PARTY.txt does not list PocketBase v${pocketbase_version}"
test -z "$(docker image inspect --format '{{range .Config.Entrypoint}}{{.}}{{end}}' "$image")" || fail 'component image sets an entrypoint'
bash -n "$work/out/bin/malrunar-teams-service"; bash -n "$work/out/lib/common.sh"
mkdir "$work/fragment"
cid="$(docker create "$image" /unused)"
docker cp "$cid:/etc/supervisor/conf.d/malrunar-teams-service.conf" "$work/fragment/" >/dev/null
docker rm -f "$cid" >/dev/null; cid=""
test "$(grep -c '^\[program:' "$work/fragment/malrunar-teams-service.conf")" = 1 || fail 'the fragment must hold exactly one [program:...] section'
grep -q '^\[program:teams-service\]' "$work/fragment/malrunar-teams-service.conf" || fail 'the program is not named after the component'
grep -q '^exitcodes=0' "$work/fragment/malrunar-teams-service.conf" || fail 'the fragment lacks exitcodes=0 (the convention for programs that cannot work)'
if grep -Eq '^(user|environment|directory)=' "$work/fragment/malrunar-teams-service.conf"; then fail 'the fragment names a user, a directory or an environment; settings come from MALRUNAR_TEAMS_SERVICE_* variables'; fi

echo '== dependencies: each one missing is reported with status 78 and its name'
expect_missing() {   # <consumer> <word> <command...>
    local consumer="$1" word="$2"; shift 2
    local status=0 out
    out="$(run "$consumer" "$@" 2>&1)" || status=$?
    echo "$out"
    [ "$status" = 78 ] || fail "'$*' without $word should exit 78, it exited $status"
    grep -qi -- "$word" <<< "$out" || fail "the message does not name $word"
}
nonode="$(variant nonode "$ubuntu_image" scratch "$msteams")"
expect_missing "$nonode" 'node' "$launcher" check
nomcp="$(variant nomcp "$ubuntu_image" "$node_image" scratch)"
expect_missing "$nomcp" 'msteams-mcp' "$launcher" check

echo '== the declared dependencies and nothing else: bare Ubuntu + node + msteams-mcp + teams-service'
bare="$(variant bare "$ubuntu_image" "$node_image" "$msteams")"
run "$bare" bash -c '! command -v supervisord >/dev/null && ! test -e /opt/malrunar/firefox && ! test -e /opt/malrunar/work-browser && ! command -v python3 >/dev/null' || fail 'the test image holds more than the declared dependencies'
run "$bare" "$launcher" check | tee "$work/check.txt"
grep -q "${pocketbase_version}" "$work/check.txt" || fail "check does not report PocketBase v${pocketbase_version}"
status=0
out="$(timeout -k 10 300 docker run --rm --entrypoint '' "$bare" "$launcher" check 2>&1)" || status=$?
[ "$status" = 78 ] || fail "as root without a user the launcher should exit 78, it exited $status"
grep -q 'MALRUNAR_TEAMS_SERVICE_USER' <<< "$out" || fail 'the message does not name MALRUNAR_TEAMS_SERVICE_USER'
run -e MALRUNAR_TEAMS_SERVICE_ENABLED=0 "$bare" "$launcher" || fail 'MALRUNAR_TEAMS_SERVICE_ENABLED=0 should exit 0'
status=0
run -e MALRUNAR_TEAMS_SERVICE_TOKEN=short "$bare" bash -c "timeout 60 $launcher; echo exit=\$?" > "$work/short.txt" 2>&1 || status=$?
grep -q 'shorter than 32' "$work/short.txt" || fail "a short token is not rejected by name: $(cat "$work/short.txt")"

echo '== unit tests inside the image: the node component runs them, the real PocketBase backs the store tests'
run -v "$here:/opt/malrunar/teams-service/test:ro" -e MALRUNAR_TEAMS_SERVICE_TEST_PB_BIN=/opt/malrunar/teams-service/bin/malrunar-teams-pocketbase "$bare" \
    bash -c 'cd /tmp && node --test /opt/malrunar/teams-service/test/*.test.mjs' | tee "$work/unit.txt" || fail 'unit tests'
grep -Eq '(#|ℹ) skipped 0' "$work/unit.txt" || fail 'a unit test was skipped (the PocketBase or msteams-mcp package is not found)'

echo '== PocketBase and the service start, the API and the schema behave, and both stop together'
run -v "$here:/test:ro" "$bare" /test/e2e/service-test.sh || fail 'service test'

echo '== the synthetic fixture server (no Microsoft, no real data)'
run -v "$here:/test:ro" "$bare" /test/e2e/fixture-test.sh || fail 'fixture test'

echo '== under supervisord on desktop-base: the fragment starts the program, as the named user'
desktop="$(variant desktop "$desktop_base" "$node_image" "$msteams")"
cat > "$work/supervised.sh" <<'SUPERVISED'
#!/usr/bin/env bash
set -euo pipefail
export MALRUNAR_TEAMS_SERVICE_USER=tester MALRUNAR_TEAMS_SERVICE_DATA_DIR=/home/tester/data
supervisord -n -c /etc/supervisor/supervisord.conf >> /tmp/supervisord.out 2>&1 &
trap 'supervisorctl -c /etc/supervisor/supervisord.conf shutdown >/dev/null 2>&1 || true; sleep 2; cat /tmp/supervisord.out' EXIT
state=''
for _ in $(seq 1 60); do
    state="$(supervisorctl -c /etc/supervisor/supervisord.conf status teams-service 2>&1 || true)"
    case "$state" in *RUNNING*) break ;; esac
    sleep 1
done
echo "$state"
case "$state" in *RUNNING*) ;; *) echo 'teams-service did not reach RUNNING'; exit 1 ;; esac
node /test/e2e/probe.mjs /home/tester/data
test "$(ps -eo user=,args= | awk '/malrunar-teams-pocketbase serve/ && !/awk/ {print $1}' | sort -u)" = tester
test "$(ps -eo user=,args= | awk '/src\/main\.mjs/ && !/awk/ {print $1}' | sort -u)" = tester
supervisorctl -c /etc/supervisor/supervisord.conf stop teams-service
SUPERVISED
chmod 755 "$work/supervised.sh"
timeout -k 10 300 docker run --rm --entrypoint '' -v "$here:/test:ro" -v "$work/supervised.sh:/supervised.sh:ro" "$desktop" /supervised.sh || fail 'supervised teams-service'
echo 'teams-service smoke passed'
