#!/usr/bin/env bash
# Inside a test consumer, as the user: the supervisord program's script, run directly. PocketBase and the
# service start, the API behaves, and both stop when the script is terminated.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MALRUNAR_TEAMS_SERVICE_DATA_DIR="$HOME/data"
log="$(mktemp)"
/opt/malrunar/teams-service/bin/malrunar-teams-service > "$log" 2>&1 &
launcher=$!
trap 'kill "$launcher" 2>/dev/null || true; cat "$log"' EXIT
node "$here/probe.mjs" "$MALRUNAR_TEAMS_SERVICE_DATA_DIR"
echo '== terminating the script stops PocketBase and the service'
kill -TERM "$launcher"
for _ in $(seq 1 30); do kill -0 "$launcher" 2>/dev/null || break; sleep 1; done
kill -0 "$launcher" 2>/dev/null && { echo 'the launcher did not stop'; exit 1; }
sleep 1
if pgrep -u "$(id -u)" -f 'malrunar-teams-pocketbase|teams-service/src/main.mjs' >/dev/null; then echo 'a child is still running'; pgrep -u "$(id -u)" -af 'malrunar-teams-pocketbase|main.mjs'; exit 1; fi
echo 'service test passed'
