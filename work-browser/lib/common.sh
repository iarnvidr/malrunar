# shellcheck shell=bash
# Shared by malrunar-work-browser and malrunar-work-browser-open (sourced, never executed).
# Every setting is an environment variable named MALRUNAR_WORK_BROWSER_*; see ../README.md.

WB_FIREFOX="${MALRUNAR_WORK_BROWSER_FIREFOX:-/opt/malrunar/firefox/firefox}"
WB_DEPENDENCY_HINT='work-browser runs the Firefox of desktop-base: build your image FROM ghcr.io/iarnvidr/desktop-base (or set MALRUNAR_WORK_BROWSER_FIREFOX to another Firefox)'
# Exit status for "this image or its settings cannot run the component" (sysexits EX_CONFIG).
WB_EX_CONFIG=78

wb_log() { printf 'malrunar-work-browser: %s\n' "$*" >&2; }
wb_die() { local status="$1"; shift; wb_log "$*"; exit "$status"; }

# Re-run the calling script as MALRUNAR_WORK_BROWSER_USER when it starts as root (the usual case under
# supervisord). Call as: wb_drop_privileges "$0" "$@". A script that starts as a normal user keeps that user.
wb_drop_privileges() {
    local self="$1"; shift
    [ "$(id -u)" = 0 ] || return 0
    local user="${MALRUNAR_WORK_BROWSER_USER:-}"
    [ -n "$user" ] || wb_die "$WB_EX_CONFIG" 'started as root: set MALRUNAR_WORK_BROWSER_USER to the desktop user that owns the browser (create that user in your own image layer)'
    id -u "$user" >/dev/null 2>&1 || wb_die "$WB_EX_CONFIG" "MALRUNAR_WORK_BROWSER_USER='$user' does not exist in this image (create the user in your own image layer)"
    command -v setpriv >/dev/null 2>&1 || wb_die "$WB_EX_CONFIG" "setpriv (util-linux) is needed to switch to '$user'. $WB_DEPENDENCY_HINT"
    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    HOME="$home" USER="$user" LOGNAME="$user" exec setpriv \
        --reuid="$(id -u "$user")" --regid="$(id -g "$user")" --init-groups -- "$self" "$@"
}

wb_require_firefox() {
    if [ ! -x "$WB_FIREFOX" ]; then
        wb_die "$WB_EX_CONFIG" "no Firefox at $WB_FIREFOX. $WB_DEPENDENCY_HINT"
    fi
}

wb_require_command() {
    command -v "$1" >/dev/null 2>&1 || wb_die "$WB_EX_CONFIG" "'$1' is missing. $WB_DEPENDENCY_HINT"
}

wb_home() {
    if [ -z "${HOME:-}" ]; then
        HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
        export HOME
    fi
    [ -n "$HOME" ] || wb_die "$WB_EX_CONFIG" 'HOME is not set and the current user has none'
}

wb_profile_dir() {
    wb_home
    printf '%s' "${MALRUNAR_WORK_BROWSER_PROFILE_DIR:-$HOME/.config/malrunar-work-browser/profile}"
}

wb_runtime_dir() {
    local dir="${MALRUNAR_WORK_BROWSER_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/malrunar-work-browser-$(id -u)}"
    mkdir -p "$dir"
    chmod 700 "$dir"
    [ -O "$dir" ] || wb_die "$WB_EX_CONFIG" "$dir is not owned by $(id -un); set MALRUNAR_WORK_BROWSER_RUNTIME_DIR"
    printf '%s' "$dir"
}

# A DISPLAY that answers: MALRUNAR_WORK_BROWSER_DISPLAY if set (the value "none" means run without
# one, for example with --headless), else $DISPLAY if it answers, else the newest answering X socket.
# Prints the display, or "none"; returns 1 when no display answers yet.
wb_find_display() {
    local candidate socket
    if [ -n "${MALRUNAR_WORK_BROWSER_DISPLAY:-}" ]; then
        printf '%s' "$MALRUNAR_WORK_BROWSER_DISPLAY"
        return 0
    fi
    if [ -n "${DISPLAY:-}" ] && xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        printf '%s' "$DISPLAY"
        return 0
    fi
    for socket in $(ls -t /tmp/.X11-unix 2>/dev/null || true); do
        case "$socket" in
            X[0-9]*) candidate=":${socket#X}" ;;
            *) continue ;;
        esac
        if xdpyinfo -display "$candidate" >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

# Apply the display found by wb_find_display to the environment.
wb_use_display() {
    local display="$1"
    if [ "$display" = none ]; then
        unset DISPLAY
    else
        export DISPLAY="$display"
    fi
}

# Firefox hands a URL to the running instance over the D-Bus session bus, so a reachable bus is
# needed. MALRUNAR_WORK_BROWSER_DBUS selects: private (default) runs a session bus of its own at a
# fixed socket path, shared by the launcher and the open helper; inherit leaves the environment alone;
# none removes DBUS_SESSION_BUS_ADDRESS.
wb_ensure_bus() {
    local mode="${MALRUNAR_WORK_BROWSER_DBUS:-private}" socket
    case "$mode" in
        inherit) return 0 ;;
        none) unset DBUS_SESSION_BUS_ADDRESS; return 0 ;;
        private) ;;
        *) wb_die "$WB_EX_CONFIG" "MALRUNAR_WORK_BROWSER_DBUS='$mode': expected private, inherit or none" ;;
    esac
    wb_require_command dbus-daemon
    wb_require_command dbus-send
    socket="$(wb_runtime_dir)/bus"
    # Probe by talking to the bus: after a restart an old socket file can be left with nothing listening.
    if ! dbus-send --bus="unix:path=$socket" --dest=org.freedesktop.DBus --type=method_call --print-reply \
            /org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
        rm -f "$socket"
        dbus-daemon --session --address="unix:path=$socket" --fork \
            || wb_die "$WB_EX_CONFIG" "could not start a private D-Bus session bus at $socket"
    fi
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$socket"
}

# Prefer-consumer profile content: MALRUNAR_WORK_BROWSER_PROFILE_SEED is a directory copied into the
# profile without overwriting anything that is already there; MALRUNAR_WORK_BROWSER_USER_JS is a
# file installed as user.js on every start (Firefox applies user.js at each start, and it wins over prefs.js).
wb_prepare_profile() {
    local profile seed="${MALRUNAR_WORK_BROWSER_PROFILE_SEED:-}" user_js="${MALRUNAR_WORK_BROWSER_USER_JS:-}"
    profile="$(wb_profile_dir)"
    mkdir -p "$profile"
    chmod 700 "$profile"
    if [ -n "$seed" ]; then
        [ -d "$seed" ] || wb_die "$WB_EX_CONFIG" "MALRUNAR_WORK_BROWSER_PROFILE_SEED='$seed' is not a directory"
        cp -rn "$seed"/. "$profile"/
    fi
    if [ -n "$user_js" ]; then
        [ -f "$user_js" ] || wb_die "$WB_EX_CONFIG" "MALRUNAR_WORK_BROWSER_USER_JS='$user_js' is not a file"
        install -m 600 "$user_js" "$profile/user.js"
    fi
}

# The pid that holds the profile lock, if any: the lock is a symlink whose target is "<host>:+<pid>".
wb_lock_owner() {
    local profile="$1" target
    target="$(readlink "$profile/lock" 2>/dev/null || true)"
    case "$target" in
        *:+[0-9]*) printf '%s' "${target##*+}" ;;
    esac
}

# Clear a lock whose owner is gone and stop leftover Firefox processes on this profile that do not
# own the lock, so a crash or a lost race cannot wedge the next start.
wb_clean_stale_profile() {
    local profile="$1" owner pid
    owner="$(wb_lock_owner "$profile")"
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
        wb_log "clearing a stale profile lock (owning pid $owner is gone)"
        rm -f "$profile/lock" "$profile/.parentlock"
        owner=""
    fi
    for pid in $(pgrep -u "$(id -u)" -f -- "-profile $profile" 2>/dev/null || true); do
        [ "$pid" = "$owner" ] && continue
        [ "$pid" = "$$" ] && continue
        wb_log "stopping a leftover Firefox process (pid $pid) on this profile"
        kill "$pid" 2>/dev/null || true
    done
}
