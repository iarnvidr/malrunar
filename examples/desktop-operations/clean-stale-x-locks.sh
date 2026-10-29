#!/bin/sh
# Remove the X lock and socket files that an earlier xrdp desktop left behind.
#
# A container that is stopped and started again (not recreated) keeps its /tmp. The stale
# /tmp/.X<n>-lock and /tmp/.X11-unix/X<n> files of the previous desktop make xrdp start the new X
# server on the next free display number (:11, :12, ...) instead of :10, and everything wired to
# :10 (session tools, health checks, screen capture) stops finding the desktop.
#
# Run this BEFORE xrdp-sesman starts, never while a desktop session is running. It only touches the
# xrdp range :10-:19, so a separate headless X server (for example Xvfb on :99) is left alone.
#
#   MALRUNAR_X_TMP_DIR   directory that holds the files (default /tmp; set it only for tests)
set -eu
dir="${MALRUNAR_X_TMP_DIR:-/tmp}"
n=10
while [ "$n" -le 19 ]; do
    rm -f "$dir/.X$n-lock" "$dir/.X11-unix/X$n"
    n=$((n + 1))
done
