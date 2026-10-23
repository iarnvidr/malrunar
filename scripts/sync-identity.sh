#!/usr/bin/env bash
# Copy the forward-auth identity module from its canonical place (tools/identity) into the components.
# Every image builds from its own directory only, so each component carries an identical vendored copy.
#   scripts/sync-identity.sh            write the copies
#   scripts/sync-identity.sh --check    fail (exit 1) if any copy is missing or differs; writes nothing
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mode=write
case "${1:-}" in
    '') ;;
    --check) mode=check ;;
    *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

src=tools/identity
# source file -> destination (the vectors go to <component>/test/forward-auth-vectors.json)
targets=(
    "forward_auth.py guacamole-tunnel/forward_auth.py"
    "forward_auth.py obs-control/malrunar_obs/forward_auth.py"
    "forward_auth.py office-convert/forward_auth.py"
    "forward-auth.mjs teams-service/src/forward-auth.mjs"
    "vectors.json guacamole-tunnel/test/forward-auth-vectors.json"
    "vectors.json obs-control/test/forward-auth-vectors.json"
    "vectors.json office-convert/test/forward-auth-vectors.json"
    "vectors.json teams-service/test/forward-auth-vectors.json"
)

status=0
for entry in "${targets[@]}"; do
    read -r name dest <<<"$entry"
    if [ "$mode" = check ]; then
        if ! cmp -s "$src/$name" "$dest"; then
            echo "out of date: $dest (edit $src/$name, run scripts/sync-identity.sh)" >&2
            status=1
        fi
    else
        install -m 0644 "$src/$name" "$dest"
        echo "wrote $dest"
    fi
done
if [ "$mode" = check ] && [ "$status" = 0 ]; then echo "identity copies are in sync"; fi
exit "$status"
