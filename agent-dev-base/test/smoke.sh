#!/usr/bin/env bash
set -euo pipefail
ref="${1:?image reference required}"
timeout -k 10 90 docker run --rm --network none --entrypoint /bin/bash "$ref" -ec '
  test "$(id -u)" = 1000
  test "$(node -p "process.versions.node.split(\".\")[0]")" = 24
  test -x /opt/malrunar/dagger/dagger
  /opt/malrunar/dagger/dagger --help >/dev/null
  command -v code-server >/dev/null
  test "$(git config --system --get maintenance.autoDetach)" = false
  test "$(git config --system --get gc.autoDetach)" = false
'
