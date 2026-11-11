#!/usr/bin/env bash
set -euo pipefail
ref="${1:?image reference required}"
pins="$(dirname "$(dirname "${BASH_SOURCE[0]}")")/pins.env"
version="$(sed -n 's/^VERSION=//p' "$pins")"
paseo_version="${version%-malrunar*}"
codex_version="$(sed -n 's/^CODEX_VERSION=//p' "$pins")"
claude_version="$(sed -n 's/^CLAUDE_CODE_VERSION=//p' "$pins")"
test -n "$paseo_version" && test -n "$codex_version" && test -n "$claude_version"
timeout -k 10 90 docker run --rm --network none \
  -e EXPECTED_PASEO_VERSION="$paseo_version" \
  -e EXPECTED_CODEX_VERSION="$codex_version" \
  -e EXPECTED_CLAUDE_VERSION="$claude_version" \
  --entrypoint /bin/bash "$ref" -ec '
  test "$(id -u)" = 1000
  test "$(paseo --version)" = "$EXPECTED_PASEO_VERSION"
  test "$(codex --version | awk "{print \$2}")" = "$EXPECTED_CODEX_VERSION"
  test "$(claude --version | awk "{print \$1}")" = "$EXPECTED_CLAUDE_VERSION"
  test "$(git config --system --get maintenance.autoDetach)" = false
  test "$(git config --system --get gc.autoDetach)" = false
'
