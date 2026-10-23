#!/usr/bin/env bash
# Inside the tools consumer, as the desktop user: a virtual X server, the REAL work-browser launcher (stock
# Firefox of desktop-base, started with WebDriver BiDi), a local web page, and `malrunar-msteams-mcp`:
# `open` hands a page to the running browser, `browser-mcp` reads, screenshots and drives its window.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool=/opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp
site="$(mktemp -d)"
mkdir -p "$site/www"
printf '<!doctype html><title>Fixture page one</title><body><h1>Hello from the fixture</h1><p>first page body</p></body>\n' > "$site/www/one.html"
printf '<!doctype html><title>Fixture page two</title><body><h1>Second fixture</h1><p>second page body</p></body>\n' > "$site/www/two.html"
cleanup() {
    kill "${wb_pid:-}" "${web_pid:-}" "${xvfb_pid:-}" 2>/dev/null || true
    sleep 1
    pkill -u "$(id -u)" -f -- '-profile' 2>/dev/null || true
    echo '--- work-browser log'; cat "$site/wb.log" 2>/dev/null || true
    rm -rf "$site"
}
trap cleanup EXIT

# The test's own X tools need a UTF-8 locale to read window titles; the component's wrapper must cope without one.
xd() { LC_ALL=C.UTF-8 xdotool "$@"; }
Xvfb :77 -screen 0 1280x800x24 -ac -nolisten tcp > "$site/xvfb.log" 2>&1 &
xvfb_pid=$!
export DISPLAY=:77
python3 -m http.server 18765 --bind 127.0.0.1 --directory "$site/www" > "$site/web.log" 2>&1 &
web_pid=$!
for _ in $(seq 1 30); do xdpyinfo -display :77 >/dev/null 2>&1 && break; sleep 0.5; done
xdpyinfo -display :77 >/dev/null 2>&1 || { echo 'Xvfb did not start'; cat "$site/xvfb.log"; exit 1; }

cat > "$site/user.js" <<'USERJS'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("app.update.auto", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
USERJS
export MALRUNAR_WORK_BROWSER_USER_JS="$site/user.js"
export MALRUNAR_WORK_BROWSER_ARGS='--remote-debugging-port=9222'
export MALRUNAR_WORK_BROWSER_START_URLS='http://127.0.0.1:18765/one.html'
export MALRUNAR_WORK_BROWSER_RETRY_SECONDS=2
/opt/malrunar/work-browser/bin/malrunar-work-browser > "$site/wb.log" 2>&1 &
wb_pid=$!

echo '== the work browser shows a window with the first page'
for _ in $(seq 1 120); do
    xd search --name 'Fixture page one' >/dev/null 2>&1 && break
    sleep 1
done
xd search --name 'Fixture page one' >/dev/null 2>&1 || { echo 'no browser window with the first page'; LC_ALL=C.UTF-8 xwininfo -root -tree | head -30; cat "$site/web.log"; exit 1; }
for _ in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/9222) 2>/dev/null && break; sleep 1; done
(exec 3<>/dev/tcp/127.0.0.1/9222) 2>/dev/null || { echo 'the BiDi port 9222 is not listening'; exit 1; }

echo '== malrunar-msteams-mcp open hands a page to the running browser (work-browser helper)'
"$tool" open 'http://127.0.0.1:18765/two.html'
for _ in $(seq 1 30); do xd search --name 'Fixture page two' >/dev/null 2>&1 && break; sleep 1; done
xd search --name 'Fixture page two' >/dev/null 2>&1 || { echo 'the page opened with `open` never appeared'; exit 1; }

echo '== browser-mcp lists windows and tabs, reads through BiDi, takes a screenshot and navigates'
# Both pages are open now (two tabs); the active one is the second.
node "$here/browser-mcp-client.mjs" 'http://127.0.0.1:18765/two.html' 'http://127.0.0.1:18765/one.html' 'Second fixture' 'Hello from the fixture'
echo 'display test passed'
