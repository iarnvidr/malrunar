# shellcheck shell=bash
# Sourced by bin/malrunar-session (never executed). Everything the kit does is in session_kit.py;
# this only turns "this image cannot run it" into a clear message before python is needed.

SK_DEPENDENCY_HINT='session-kit runs on desktop-base (python3, the X11 utilities and XFCE): build your image FROM ghcr.io/iarnvidr/desktop-base'
# Exit status for "this image or its settings cannot run the component" (sysexits EX_CONFIG).
SK_EX_CONFIG=78

sk_die() { local status="$1"; shift; printf 'malrunar-session-kit: %s\n' "$*" >&2; exit "$status"; }

sk_require_python() {
    command -v python3 >/dev/null 2>&1 || sk_die "$SK_EX_CONFIG" "python3 is missing. $SK_DEPENDENCY_HINT"
}
