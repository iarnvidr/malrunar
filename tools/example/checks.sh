#!/usr/bin/env bash
# Checks run inside the assembled image.
set -euo pipefail
test "$(cat /etc/example-base)" = "example base"
test -f /opt/example/alpha/README
test "$(/opt/example/beta/bin/beta)" = beta
test "$(tail -n1 /opt/example/gamma/gamma.txt)" = gamma
test "$EXAMPLE_HOME" = /opt/example
echo "example checks passed"
