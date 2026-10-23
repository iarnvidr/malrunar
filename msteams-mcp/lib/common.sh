# shellcheck shell=bash
# Shared by malrunar-msteams-mcp (sourced, never executed). Every setting is an environment variable
# named MALRUNAR_MSTEAMS_MCP_*; see ../README.md.

MS_ROOT="${MS_ROOT:-/opt/malrunar/msteams-mcp}"
MS_APP="$MS_ROOT/app"
# Exit status for "this image or its settings cannot run the component" (sysexits EX_CONFIG).
MS_EX_CONFIG=78
MS_NODE_HINT='the node component is missing: add COPY --link --from=ghcr.io/iarnvidr/node:<tag> / / and put /opt/malrunar/node/bin on PATH (or set MALRUNAR_MSTEAMS_MCP_NODE)'
MS_DESKTOP_HINT='desktop-base is missing: build your image FROM ghcr.io/iarnvidr/desktop-base (it provides Firefox and the libraries of the Playwright browser)'
MS_WORK_BROWSER_HINT='the work-browser component is missing: add COPY --link --from=ghcr.io/iarnvidr/work-browser:<tag> / /'

ms_log() { printf 'malrunar-msteams-mcp: %s\n' "$*" >&2; }
ms_die() { local status="$1"; shift; ms_log "$*"; exit "$status"; }

# Re-run the calling script as the desktop user when it starts as root. The token cache key is derived
# from the host name and the USER NAME, so every command of this component (the MCP server, the sign-in,
# the Teams service) must run as the same user. MALRUNAR_MSTEAMS_MCP_USER, else MALRUNAR_WORK_BROWSER_USER.
# MALRUNAR_MSTEAMS_MCP_ALLOW_ROOT=1 keeps root (then root is the identity the session belongs to).
ms_drop_privileges() {
    local self="$1"; shift
    [ "$(id -u)" = 0 ] || return 0
    [ "${MALRUNAR_MSTEAMS_MCP_ALLOW_ROOT:-0}" != 1 ] || return 0
    local user="${MALRUNAR_MSTEAMS_MCP_USER:-${MALRUNAR_WORK_BROWSER_USER:-}}"
    [ -n "$user" ] || ms_die "$MS_EX_CONFIG" 'started as root: set MALRUNAR_MSTEAMS_MCP_USER (or MALRUNAR_WORK_BROWSER_USER) to the desktop user that owns the Teams session; its name is part of the token cache key. MALRUNAR_MSTEAMS_MCP_ALLOW_ROOT=1 runs it as root on purpose'
    id -u "$user" >/dev/null 2>&1 || ms_die "$MS_EX_CONFIG" "user '$user' does not exist in this image (create the user in your own image layer)"
    command -v setpriv >/dev/null 2>&1 || ms_die "$MS_EX_CONFIG" "setpriv (util-linux) is needed to switch to '$user'. $MS_DESKTOP_HINT"
    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    HOME="$home" USER="$user" LOGNAME="$user" exec setpriv \
        --reuid="$(id -u "$user")" --regid="$(id -g "$user")" --init-groups -- "$self" "$@"
}

ms_home() {
    if [ -z "${HOME:-}" ]; then
        HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
        export HOME
    fi
    [ -n "$HOME" ] || ms_die "$MS_EX_CONFIG" 'HOME is not set and the current user has none'
}

# --- dependencies --------------------------------------------------------------------------------

ms_find_node() {
    if [ -n "${MALRUNAR_MSTEAMS_MCP_NODE:-}" ]; then
        [ -x "$MALRUNAR_MSTEAMS_MCP_NODE" ] || ms_die "$MS_EX_CONFIG" "MALRUNAR_MSTEAMS_MCP_NODE='$MALRUNAR_MSTEAMS_MCP_NODE' is not an executable"
        MS_NODE="$MALRUNAR_MSTEAMS_MCP_NODE"
    elif [ -x /opt/malrunar/node/bin/node ]; then
        MS_NODE=/opt/malrunar/node/bin/node
    elif command -v node >/dev/null 2>&1; then
        MS_NODE="$(command -v node)"
    else
        ms_die "$MS_EX_CONFIG" "no node found. $MS_NODE_HINT"
    fi
    local major
    major="$("$MS_NODE" -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
    case "$major" in ''|*[!0-9]*) ms_die "$MS_EX_CONFIG" "$MS_NODE does not run";; esac
    [ "$major" -ge 22 ] || ms_die "$MS_EX_CONFIG" "$MS_NODE is Node $major; msteams-mcp needs Node 22 or newer (node:sqlite), the node component ships 24"
    export MS_NODE
}

ms_require_package() {
    [ -r "$MS_APP/dist/index.js" ] && [ -r "$MS_APP/dist/cli.js" ] || ms_die "$MS_EX_CONFIG" "the msteams-mcp package is missing from $MS_APP (an incomplete copy of the component?)"
}

ms_require_desktop_base() {
    if [ ! -x /opt/malrunar/firefox/firefox ] || [ ! -d /usr/local/share/malrunar/desktop-base ]; then
        ms_die "$MS_EX_CONFIG" "no desktop-base. $MS_DESKTOP_HINT"
    fi
}

ms_require_work_browser() {
    [ -x /opt/malrunar/work-browser/bin/malrunar-work-browser-open ] || ms_die "$MS_EX_CONFIG" "no work-browser. $MS_WORK_BROWSER_HINT"
}

# node + desktop-base + work-browser, and the Playwright browser can load its libraries. Called by every
# command: the three components are this component's declared dependencies (README, "Dependencies").
ms_require_all() {
    ms_find_node
    ms_require_package
    ms_require_desktop_base
    ms_require_work_browser
    ms_find_playwright_firefox
}

ms_find_playwright_firefox() {
    local candidate
    for candidate in "$MS_ROOT"/browsers/firefox-*/firefox/firefox; do
        [ -x "$candidate" ] && MS_PLAYWRIGHT_FIREFOX="$candidate" && export MS_PLAYWRIGHT_FIREFOX && return 0
    done
    ms_die "$MS_EX_CONFIG" "Playwright's Firefox is missing from $MS_ROOT/browsers (an incomplete copy of the component?)"
}

# Libraries the Playwright Firefox needs that this image does not have (desktop-base has them all).
ms_missing_libraries() {
    command -v ldd >/dev/null 2>&1 || return 0
    ldd "$MS_PLAYWRIGHT_FIREFOX" 2>/dev/null | sed -n 's/^[[:space:]]*\(.*\) => not found.*/\1/p'
}

# Tools of the optional visual browser MCP (browser-mcp); they are not part of desktop-base.
ms_missing_browser_mcp_tools() {
    local tool
    for tool in xdotool xprop xdpyinfo import xclip pdftotext python3; do
        command -v "$tool" >/dev/null 2>&1 || printf '%s\n' "$tool"
    done
}

# --- environment of the package ------------------------------------------------------------------

# Map the component's MALRUNAR_MSTEAMS_MCP_* settings onto the variables the patched package reads.
ms_export_package_env() {
    export PLAYWRIGHT_BROWSERS_PATH="$MS_ROOT/browsers"
    export PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH="${MALRUNAR_MSTEAMS_MCP_FIREFOX:-$MS_PLAYWRIGHT_FIREFOX}"
    # The host check of Playwright only knows the Ubuntu releases it was published for.
    export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
    [ -z "${MALRUNAR_MSTEAMS_MCP_LOGIN_TIMEOUT_MS:-}" ] || export MSTEAMS_LOGIN_TIMEOUT_MS="$MALRUNAR_MSTEAMS_MCP_LOGIN_TIMEOUT_MS"
    [ -z "${MALRUNAR_MSTEAMS_MCP_TEAMS_URL:-}" ] || export MSTEAMS_TEAMS_URL="$MALRUNAR_MSTEAMS_MCP_TEAMS_URL"
    [ -z "${MALRUNAR_MSTEAMS_MCP_TOKEN_WAIT_MS:-}" ] || export MSTEAMS_TOKEN_REFRESH_WAIT_MS="$MALRUNAR_MSTEAMS_MCP_TOKEN_WAIT_MS"
    export MALRUNAR_MSTEAMS_MCP_PKG="${MALRUNAR_MSTEAMS_MCP_PKG:-$MS_APP/dist}"
    export MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR
    MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR="$(ms_browser_profile_dir)"
}

# `info` and `debug` log to STDOUT, which is the MCP transport: only warn and error are safe.
ms_server_log_level() {
    local level="${MALRUNAR_MSTEAMS_MCP_LOG_LEVEL:-warn}"
    case "$level" in
        warn|error) ;;
        *) ms_log "MALRUNAR_MSTEAMS_MCP_LOG_LEVEL=$level would write to stdout, the MCP transport; using warn"; level=warn ;;
    esac
    printf '%s' "$level"
}

# --- the visible browser (work-browser) ----------------------------------------------------------

# The profile of the visible browser: the work-browser component's own setting and default.
ms_browser_profile_dir() {
    ms_home
    printf '%s' "${MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR:-${MALRUNAR_WORK_BROWSER_PROFILE_DIR:-$HOME/.config/malrunar-work-browser/profile}}"
}

# The package's own browser profile (upstream fixes it under the home directory).
ms_playwright_profile_dir() {
    ms_home
    printf '%s' "$HOME/.teams-mcp-server/browser-profile"
}

# True when the visible browser and the package's own browser use the same profile directory.
ms_profiles_shared() {
    local a b
    a="$(readlink -f "$(ms_playwright_profile_dir)" 2>/dev/null || ms_playwright_profile_dir)"
    b="$(readlink -f "$MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR" 2>/dev/null || printf '%s' "$MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR")"
    [ "$a" = "$b" ]
}

# A display that answers, using the work-browser component's own search.
ms_find_display() {
    # shellcheck source=/dev/null
    [ -r /opt/malrunar/work-browser/lib/common.sh ] && . /opt/malrunar/work-browser/lib/common.sh
    if declare -F wb_find_display >/dev/null; then wb_find_display; return; fi
    return 1
}

# Free a profile so it can be written: stop the work browser. `supervisor` uses supervisorctl for the
# program of the work-browser component (needs access to the supervisord socket, usually root);
# `kill` ends the Firefox that holds the profile and lets the launcher restart it after its retry
# delay; `none` does nothing (you stopped it). `auto` is supervisor when it works, else kill.
MS_BROWSER_RESTART=''
ms_supervisorctl() { supervisorctl -c "${MALRUNAR_MSTEAMS_MCP_SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}" "$@"; }

ms_browser_release() {
    local profile="$1" mode="${MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL:-auto}"
    local program="${MALRUNAR_MSTEAMS_MCP_BROWSER_PROGRAM:-work-browser}"
    case "$mode" in auto|supervisor|kill|none) ;; *) ms_die "$MS_EX_CONFIG" "MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL must be auto, supervisor, kill or none, not '$mode'";; esac
    if [ "$mode" = auto ]; then
        if command -v supervisorctl >/dev/null 2>&1 && ms_supervisorctl status "$program" >/dev/null 2>&1; then mode='supervisor'; else mode='kill'; fi
    fi
    case "$mode" in
        supervisor)
            command -v supervisorctl >/dev/null 2>&1 || ms_die "$MS_EX_CONFIG" 'supervisorctl is not available (desktop-base provides supervisor)'
            ms_supervisorctl stop "$program" >&2 || ms_die 1 "could not stop the supervisord program '$program'"
            MS_BROWSER_RESTART="$program"
            ;;
        kill)
            local tries=0
            pkill -f -- "-profile $profile" 2>/dev/null || true
            while pgrep -f -- "-profile $profile" >/dev/null 2>&1; do
                tries=$((tries + 1))
                [ "$tries" -le 50 ] || ms_die 1 "a Firefox still holds $profile"
                sleep 0.2
            done
            ;;
        none) ;;
    esac
}

ms_browser_restore() {
    if [ -n "$MS_BROWSER_RESTART" ]; then
        ms_supervisorctl start "$MS_BROWSER_RESTART" >&2 || ms_log "could not start the supervisord program '$MS_BROWSER_RESTART' again"
        MS_BROWSER_RESTART=''
    fi
}

# Firefox refuses to open a profile last written by a NEWER version. When the visible browser and the
# package share one profile (not the default) the two builds differ, so clear the version stamp of a
# profile that a different build wrote. compatibility.ini only records version and location; Firefox
# rewrites it at the next start. No cookies, logins or other data live in it.
ms_reconcile_profile_version() {
    local profile="$1" firefox="$2" ini="$1/compatibility.ini" stamped running
    [ -f "$ini" ] || return 0
    running="$("$firefox" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
    stamped="$(sed -n 's/^LastVersion=\([0-9][0-9.]*\)_.*/\1/p' "$ini" | head -1)"
    [ -n "$running" ] && [ -n "$stamped" ] || return 0
    if [ "$running" != "$stamped" ]; then
        ms_log "profile was last used by Firefox $stamped, launching $running; clearing the version stamp"
        rm -f "$ini"
    fi
}
