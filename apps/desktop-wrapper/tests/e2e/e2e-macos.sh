#!/usr/bin/env bash
# Launches a built .app whose address is http://127.0.0.1:PORT/ against tests/e2e/server.py and checks
# that the page loads in the web view and that its Notification and setAppBadge calls reach the app.
#   tests/e2e/e2e-macos.sh path/to/Name.app PORT OUTDIR
# macOS only. It cannot see the banner itself; it checks that the app accepted the notification.
set -euo pipefail

app="$1"
port="$2"
out="$3"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$out"
report="$out/server.jsonl"
: > "$report"

python3 "$here/server.py" "$port" "$report" &
server=$!
executable="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist")"
"$app/Contents/MacOS/$executable" > "$out/app.log" 2>&1 &
pid=$!
cleanup() { kill "$pid" "$server" 2>/dev/null || true; wait "$pid" "$server" 2>/dev/null || true; }
trap cleanup EXIT

result=''
for _ in $(seq 1 90); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "the app exited early; its log:" >&2
    cat "$out/app.log" >&2
    exit 1
  fi
  if grep -q '"report"' "$report"; then result="$(grep '"report"' "$report" | tail -n 1)"; break; fi
  sleep 1
done
screencapture -x "$out/screen.png" 2>/dev/null || true

echo "server log:"; cat "$report"
echo "app log:"; cat "$out/app.log"
if [ -z "$result" ]; then echo "no report from the page within 90 s" >&2; exit 1; fi
python3 - "$result" <<'PY'
import json, sys
report = json.loads(sys.argv[1])["report"]
expected = {"permission": "granted", "internals": "object", "shown": True, "badge": "ok"}
bad = {key: report.get(key) for key, value in expected.items() if report.get(key) != value}
if bad:
    sys.exit(f"unexpected page report: {bad} (full report: {report})")
print("page report ok:", report)
PY
