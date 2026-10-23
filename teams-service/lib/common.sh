# shellcheck shell=bash
# Shared by malrunar-teams-service (sourced, never executed). Every setting is an environment variable
# named MALRUNAR_TEAMS_SERVICE_*; see ../README.md.

TS_ROOT="${TS_ROOT:-/opt/malrunar/teams-service}"
# The package of the msteams-mcp component whose API modules the service imports.
TS_MCP_DIST="${MALRUNAR_TEAMS_SERVICE_MCP_DIST:-/opt/malrunar/msteams-mcp/app/dist}"
# Exit status for "this image or its settings cannot run the component" (sysexits EX_CONFIG).
TS_EX_CONFIG=78
TS_NODE_HINT='the node component is missing: add COPY --link --from=ghcr.io/iarnvidr/node:<tag> / / (or set MALRUNAR_TEAMS_SERVICE_NODE)'
TS_MCP_HINT='the msteams-mcp component is missing: add COPY --link --from=ghcr.io/iarnvidr/msteams-mcp:<tag> / / (the service imports its package and reads the session it signed in; or set MALRUNAR_TEAMS_SERVICE_MCP_DIST)'

ts_log() { printf 'malrunar-teams-service: %s\n' "$*" >&2; }
ts_die() { local status="$1"; shift; ts_log "$*"; exit "$status"; }

# Re-run the calling script as the user that owns the Teams session (when it starts as root, the usual
# case under supervisord). The session is encrypted with a key derived from the host name and the USER
# NAME, so this must be the user that ran the msteams-mcp sign-in.
ts_drop_privileges() {
    local self="$1"; shift
    [ "$(id -u)" = 0 ] || return 0
    [ "${MALRUNAR_TEAMS_SERVICE_ALLOW_ROOT:-0}" != 1 ] || return 0
    local user="${MALRUNAR_TEAMS_SERVICE_USER:-${MALRUNAR_MSTEAMS_MCP_USER:-${MALRUNAR_WORK_BROWSER_USER:-}}}"
    [ -n "$user" ] || ts_die "$TS_EX_CONFIG" 'started as root: set MALRUNAR_TEAMS_SERVICE_USER (or MALRUNAR_MSTEAMS_MCP_USER) to the user that signed in to Teams; its name is part of the key that protects the session. MALRUNAR_TEAMS_SERVICE_ALLOW_ROOT=1 runs it as root on purpose'
    id -u "$user" >/dev/null 2>&1 || ts_die "$TS_EX_CONFIG" "user '$user' does not exist in this image (create the user in your own image layer)"
    command -v setpriv >/dev/null 2>&1 || ts_die "$TS_EX_CONFIG" "setpriv (util-linux) is needed to switch to '$user'"
    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    HOME="$home" USER="$user" LOGNAME="$user" exec setpriv \
        --reuid="$(id -u "$user")" --regid="$(id -g "$user")" --init-groups -- "$self" "$@"
}

ts_home() {
    if [ -z "${HOME:-}" ]; then
        HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
        export HOME
    fi
    [ -n "$HOME" ] || ts_die "$TS_EX_CONFIG" 'HOME is not set and the current user has none'
}

ts_find_node() {
    if [ -n "${MALRUNAR_TEAMS_SERVICE_NODE:-}" ]; then
        [ -x "$MALRUNAR_TEAMS_SERVICE_NODE" ] || ts_die "$TS_EX_CONFIG" "MALRUNAR_TEAMS_SERVICE_NODE='$MALRUNAR_TEAMS_SERVICE_NODE' is not an executable"
        TS_NODE="$MALRUNAR_TEAMS_SERVICE_NODE"
    elif [ -x /opt/malrunar/node/bin/node ]; then
        TS_NODE=/opt/malrunar/node/bin/node
    elif command -v node >/dev/null 2>&1; then
        TS_NODE="$(command -v node)"
    else
        ts_die "$TS_EX_CONFIG" "no node found. $TS_NODE_HINT"
    fi
    local major
    major="$("$TS_NODE" -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
    case "$major" in ''|*[!0-9]*) ts_die "$TS_EX_CONFIG" "$TS_NODE does not run";; esac
    [ "$major" -ge 22 ] || ts_die "$TS_EX_CONFIG" "$TS_NODE is Node $major; the service needs Node 22 or newer, the node component ships 24"
    export TS_NODE
}

ts_require_mcp_package() {
    [ -r "$TS_MCP_DIST/auth/session-store.js" ] || ts_die "$TS_EX_CONFIG" "no msteams-mcp package at $TS_MCP_DIST. $TS_MCP_HINT"
}
