# shellcheck shell=bash
# Shared by malrunar-thunderbird (sourced, never executed).
# Every setting is an environment variable named MALRUNAR_THUNDERBIRD_*; see ../README.md.

TB_APP="${MALRUNAR_THUNDERBIRD_APP:-/opt/malrunar/thunderbird/app}"
TB_DEPENDENCY_HINT='thunderbird runs on the libraries of desktop-base: build your image FROM ghcr.io/iarnvidr/desktop-base'
# Exit status for "this image or its settings cannot run the component" (sysexits EX_CONFIG).
TB_EX_CONFIG=78

tb_log() { printf 'malrunar-thunderbird: %s\n' "$*" >&2; }
tb_die() { local status="$1"; shift; tb_log "$*"; exit "$status"; }

# Re-run the calling script as MALRUNAR_THUNDERBIRD_USER when it starts as root (the usual case under
# supervisord). Call as: tb_drop_privileges "$0" "$@". A script that starts as a normal user keeps that user.
tb_drop_privileges() {
    local self="$1"; shift
    [ "$(id -u)" = 0 ] || return 0
    local user="${MALRUNAR_THUNDERBIRD_USER:-}"
    [ -n "$user" ] || tb_die "$TB_EX_CONFIG" 'started as root: set MALRUNAR_THUNDERBIRD_USER to the desktop user that owns the mailbox profile (create that user in your own image layer)'
    id -u "$user" >/dev/null 2>&1 || tb_die "$TB_EX_CONFIG" "MALRUNAR_THUNDERBIRD_USER='$user' does not exist in this image (create the user in your own image layer)"
    command -v setpriv >/dev/null 2>&1 || tb_die "$TB_EX_CONFIG" "setpriv (util-linux) is needed to switch to '$user'. $TB_DEPENDENCY_HINT"
    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    HOME="$home" USER="$user" LOGNAME="$user" exec setpriv \
        --reuid="$(id -u "$user")" --regid="$(id -g "$user")" --init-groups -- "$self" "$@"
}

# Thunderbird is useless without the libraries desktop-base installs; say so by name instead of letting
# the dynamic loader fail with a bare "cannot open shared object file".
tb_require_runtime() {
    if [ ! -x "$TB_APP/thunderbird" ]; then
        tb_die "$TB_EX_CONFIG" "no Thunderbird at $TB_APP/thunderbird (MALRUNAR_THUNDERBIRD_APP)"
    fi
    local lib libs
    libs="$(ldconfig -p 2>/dev/null || true)"
    for lib in libgtk-3.so.0 libnss3.so libasound.so.2 libdbus-glib-1.so.2 libXt.so.6; do
        if ! grep -q "$lib" <<< "$libs"; then
            tb_die "$TB_EX_CONFIG" "$lib is missing. $TB_DEPENDENCY_HINT"
        fi
    done
    command -v xdpyinfo >/dev/null 2>&1 || [ "${MALRUNAR_THUNDERBIRD_DISPLAY:-}" = none ] \
        || tb_die "$TB_EX_CONFIG" "xdpyinfo (x11-utils) is missing. $TB_DEPENDENCY_HINT"
}

tb_home() {
    if [ -z "${HOME:-}" ]; then
        HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
        export HOME
    fi
    [ -n "$HOME" ] || tb_die "$TB_EX_CONFIG" 'HOME is not set and the current user has none'
}

tb_profile_dir() {
    tb_home
    printf '%s' "${MALRUNAR_THUNDERBIRD_PROFILE_DIR:-$HOME/.config/malrunar-thunderbird/profile}"
}

# A DISPLAY that answers: MALRUNAR_THUNDERBIRD_DISPLAY if set (the value "none" means run headless,
# without one), else $DISPLAY if it answers, else the newest answering X socket. Prints the display, or
# "none"; returns 1 when no display answers yet.
tb_find_display() {
    local candidate socket
    if [ -n "${MALRUNAR_THUNDERBIRD_DISPLAY:-}" ]; then
        printf '%s' "$MALRUNAR_THUNDERBIRD_DISPLAY"
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

# Create the profile (mode 700) and install the consumer's user.js, if one is named.
tb_prepare_profile() {
    local profile user_js="${MALRUNAR_THUNDERBIRD_USER_JS:-}"
    profile="$(tb_profile_dir)"
    mkdir -p "$profile"
    chmod 700 "$profile"
    if [ -n "$user_js" ]; then
        [ -f "$user_js" ] || tb_die "$TB_EX_CONFIG" "MALRUNAR_THUNDERBIRD_USER_JS='$user_js' is not a file"
        install -m 600 "$user_js" "$profile/user.js"
    fi
}

# The pid that holds the profile lock, if any: the lock is a symlink whose target is "<host>:+<pid>".
tb_lock_owner() {
    local profile="$1" target
    target="$(readlink "$profile/lock" 2>/dev/null || true)"
    case "$target" in
        *:+[0-9]*) printf '%s' "${target##*+}" ;;
    esac
}

# Clear a lock whose owner is gone, so a crash cannot wedge the next start.
tb_clean_stale_profile() {
    local profile="$1" owner
    owner="$(tb_lock_owner "$profile")"
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
        tb_log "clearing a stale profile lock (owning pid $owner is gone)"
        rm -f "$profile/lock" "$profile/.parentlock"
    fi
}
