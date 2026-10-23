#!/usr/bin/env bash
# Inside the full test consumer, as the desktop user: `malrunar-msteams-mcp sync-browser` against a REAL
# Firefox profile. A headless stock Firefox (the work-browser launcher) creates the profile and its
# cookies.sqlite; a session with a Microsoft cookie jar is written the way the package writes it
# (encrypted, in the user's home); sync-browser copies the Microsoft cookies into the profile; the
# browser then starts again on that profile and stays up.
set -euo pipefail
tool=/opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp
profile="$HOME/.config/malrunar-work-browser/profile"
log="$(mktemp)"
cleanup() { pkill -u "$(id -u)" -f -- '-profile' 2>/dev/null || true; rm -f "$log"; }
trap cleanup EXIT

start_browser() {
    MALRUNAR_WORK_BROWSER_DISPLAY=none MALRUNAR_WORK_BROWSER_ARGS=--headless MALRUNAR_WORK_BROWSER_RETRY_SECONDS=1 \
        /opt/malrunar/work-browser/bin/malrunar-work-browser >> "$log" 2>&1 &
    browser_launcher=$!
}

echo '== a headless Firefox creates the profile'
start_browser
for _ in $(seq 1 90); do [ -s "$profile/cookies.sqlite" ] && break; sleep 1; done
[ -s "$profile/cookies.sqlite" ] || { echo 'Firefox never created cookies.sqlite'; cat "$log"; exit 1; }
kill "$browser_launcher" 2>/dev/null || true
pkill -u "$(id -u)" -f -- "-profile $profile" 2>/dev/null || true
for _ in $(seq 1 30); do pgrep -u "$(id -u)" -f -- "-profile $profile" >/dev/null 2>&1 || break; sleep 0.5; done

echo '== a session with a Microsoft cookie jar, written by the package itself'
node - <<'NODE'
const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const { writeSessionState } = await import(`${PKG}/auth/session-store.js`);
const exp = Math.floor(Date.now() / 1000) + 86400;
writeSessionState({
  cookies: [
    { name: 'ESTSAUTHPERSISTENT', value: 'sso-value-1', domain: '.login.microsoftonline.com', path: '/', expires: exp, httpOnly: true, secure: true, sameSite: 'None' },
    { name: 'esctx', value: 'ctx-1', domain: '.login.microsoftonline.com', path: '/', expires: -1, httpOnly: true, secure: true, sameSite: 'None' },
    { name: 'authtoken', value: 'teams-1', domain: 'teams.cloud.microsoft', path: '/', expires: exp, httpOnly: false, secure: true, sameSite: 'Lax' },
    { name: 'unrelated', value: 'must-not-be-copied', domain: 'example.org', path: '/', expires: exp, httpOnly: false, secure: false, sameSite: 'None' },
  ],
  origins: [],
});
NODE

echo '== dry run changes nothing'
out="$("$tool" sync-browser --dry-run)"; echo "$out"
case "$out" in *'Mirroring 3 Microsoft cookies'*'DRY_RUN'*) ;; *) echo 'unexpected dry-run output'; exit 1 ;; esac

echo '== sync-browser writes the Microsoft cookies into the profile'
out="$("$tool" sync-browser)"; echo "$out"
case "$out" in *'SYNC_DONE wrote 3 Microsoft cookies'*) ;; *) echo 'unexpected sync output'; exit 1 ;; esac
node - "$profile/cookies.sqlite" <<'NODE'
import { DatabaseSync } from 'node:sqlite';
const db = new DatabaseSync(process.argv[2], { readOnly: true });
const rows = db.prepare('SELECT name, value, host, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies ORDER BY name').all();
db.close();
const byName = Object.fromEntries(rows.map(r => [r.name, r]));
const expect = (ok, what) => { if (!ok) { console.error('FAILED: ' + what, JSON.stringify(rows)); process.exit(1); } };
expect(byName.ESTSAUTHPERSISTENT?.value === 'sso-value-1' && byName.ESTSAUTHPERSISTENT.host === '.login.microsoftonline.com', 'ESTSAUTHPERSISTENT');
expect(byName.ESTSAUTHPERSISTENT.isSecure === 1 && byName.ESTSAUTHPERSISTENT.isHttpOnly === 1, 'flags');
expect(byName.esctx?.expiry > Date.now() / 1000, 'a session cookie gets a lifetime');
expect(byName.authtoken?.sameSite === 1, 'SameSite=Lax');
expect(!byName.unrelated, 'a cookie of another site was copied');
console.log('cookies in the real profile:', rows.map(r => r.name).join(', '));
NODE

echo '== Firefox starts on the written profile and stays up'
start_browser
sleep 10
kill -0 "$browser_launcher" 2>/dev/null || { echo 'the launcher exited'; cat "$log"; exit 1; }
pgrep -u "$(id -u)" -f -- "-profile $profile" >/dev/null || { echo 'Firefox is not running on the profile'; cat "$log"; exit 1; }
if grep -qi 'corrupt\|database disk image is malformed' "$log"; then echo 'Firefox reports a corrupt database'; cat "$log"; exit 1; fi
echo 'sync test passed'
