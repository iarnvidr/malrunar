#!/usr/bin/env bash
set -euo pipefail
ref="${1:?image reference required}"
timeout -k 10 90 docker run --rm --network none --entrypoint /bin/bash "$ref" -ec '
  test "$(id -u)" = 1000
  test "$(paseo --version)" = 0.14.0
  test "$(git config --system --get maintenance.autoDetach)" = false
  test "$(git config --system --get gc.autoDetach)" = false
  test ! -e /opt/ginnungagap
'
