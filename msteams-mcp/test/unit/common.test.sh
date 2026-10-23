#!/usr/bin/env bash
# Behaviour tests of lib/common.sh (settings, log level, profile paths, the version stamp repair, the
# tool check). Needs only bash and coreutils, so it runs on any host as well as in the consumer image.
#   common.test.sh [<component-dir>]    default: the directory two levels above this script
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
component="${1:-$(cd "$here/../.." && pwd)}"
fail() { echo "common.sh test failed: $*" >&2; exit 1; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# shellcheck source=/dev/null
. "$component/lib/common.sh"
export HOME="$work/home"
mkdir -p "$HOME"

echo '-- log level: only warn and error may reach stdout-safe output'
unset MALRUNAR_MSTEAMS_MCP_LOG_LEVEL
[ "$(ms_server_log_level)" = warn ] || fail 'default level is not warn'
export MALRUNAR_MSTEAMS_MCP_LOG_LEVEL=error; [ "$(ms_server_log_level)" = error ] || fail 'error is refused'
export MALRUNAR_MSTEAMS_MCP_LOG_LEVEL=info; [ "$(ms_server_log_level 2>/dev/null)" = warn ] || fail 'info must be turned into warn'
export MALRUNAR_MSTEAMS_MCP_LOG_LEVEL=debug; ms_server_log_level 2>&1 >/dev/null | grep -q 'stdout' || fail 'the refusal of debug says nothing about stdout'
unset MALRUNAR_MSTEAMS_MCP_LOG_LEVEL

echo '-- the visible browser profile follows the work-browser component, and can be overridden'
unset MALRUNAR_WORK_BROWSER_PROFILE_DIR MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR
[ "$(ms_browser_profile_dir)" = "$HOME/.config/malrunar-work-browser/profile" ] || fail "default profile dir is $(ms_browser_profile_dir)"
export MALRUNAR_WORK_BROWSER_PROFILE_DIR=/data/wb; [ "$(ms_browser_profile_dir)" = /data/wb ] || fail 'does not follow MALRUNAR_WORK_BROWSER_PROFILE_DIR'
export MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR=/data/own; [ "$(ms_browser_profile_dir)" = /data/own ] || fail 'MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR does not win'
[ "$(ms_playwright_profile_dir)" = "$HOME/.teams-mcp-server/browser-profile" ] || fail 'the package profile is not where upstream keeps it'

echo '-- shared or separate profiles'
export MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR="$HOME/.teams-mcp-server/browser-profile"
ms_profiles_shared || fail 'the same directory is not detected as shared'
export MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR="$work/elsewhere"
! ms_profiles_shared || fail 'different directories are detected as shared'

echo '-- version stamp: cleared only when the builds differ'
profile="$work/profile"; mkdir -p "$profile"
printf '#!/usr/bin/env bash\necho "Mozilla Firefox 146.0.1"\n' > "$work/firefox"; chmod +x "$work/firefox"
printf 'Version=153.5\nLastVersion=153.5_20261001000000/20261001000000\n' > "$profile/compatibility.ini"
ms_reconcile_profile_version "$profile" "$work/firefox" 2>/dev/null
[ ! -e "$profile/compatibility.ini" ] || fail 'a stamp of a newer build was kept'
printf 'LastVersion=146.0_20261001000000/20261001000000\n' > "$profile/compatibility.ini"
ms_reconcile_profile_version "$profile" "$work/firefox" 2>/dev/null
[ -e "$profile/compatibility.ini" ] || fail 'a stamp of the same build was removed'
rm "$profile/compatibility.ini"; ms_reconcile_profile_version "$profile" "$work/firefox"   # no stamp: no error

echo '-- optional tools of browser-mcp are named when missing'
mkdir -p "$work/bin"
for tool in xprop xdpyinfo import python3; do printf '#!/bin/sh\n' > "$work/bin/$tool"; chmod +x "$work/bin/$tool"; done
missing="$(PATH="$work/bin" ms_missing_browser_mcp_tools | tr '\n' ' ')"
case "$missing" in *xdotool*xclip*pdftotext*) ;; *) fail "missing tools reported as '$missing'" ;; esac
case "$missing" in *xprop*|*python3*) fail "present tools reported missing: '$missing'" ;; esac

echo '-- the browser is released and restored through the right mode'
mkdir -p "$work/sbin"
cat > "$work/sbin/supervisorctl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case "$*" in *status*) [ "${STUB_STATUS_FAILS:-0}" = 1 ] && exit 3; echo 'work-browser RUNNING' ;; esac
exit 0
STUB
chmod +x "$work/sbin/supervisorctl"
export STUB_LOG="$work/log"; : > "$STUB_LOG"
old_path="$PATH"; PATH="$work/sbin:$PATH"
MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=auto ms_browser_release "$profile"
grep -q 'stop work-browser' "$STUB_LOG" || fail 'auto mode did not stop the supervisord program'
ms_browser_restore
grep -q 'start work-browser' "$STUB_LOG" || fail 'the program was not started again'
: > "$STUB_LOG"; STUB_STATUS_FAILS=1 MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=auto ms_browser_release "$profile"
! grep -q 'stop' "$STUB_LOG" || fail 'auto mode used supervisorctl although it cannot reach supervisord'
: > "$STUB_LOG"; MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=none ms_browser_release "$profile"
[ ! -s "$STUB_LOG" ] || fail 'none mode touched supervisord'
MALRUNAR_MSTEAMS_MCP_BROWSER_PROGRAM=my-browser MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=supervisor ms_browser_release "$profile"
grep -q 'stop my-browser' "$STUB_LOG" || fail 'MALRUNAR_MSTEAMS_MCP_BROWSER_PROGRAM is ignored'
ms_browser_restore
PATH="$old_path"
out="$( ( MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL=bogus ms_browser_release "$profile" ) 2>&1 || true )"
case "$out" in *'must be auto'*) ;; *) fail "a bad mode is accepted: $out" ;; esac
echo 'common.sh tests passed'
