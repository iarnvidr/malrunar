#!/usr/bin/env bash
# Run an image's smoke test against its locally built image.
#   scripts/smoke.sh <image> [image-ref]
# image-ref defaults to the local h-<hash> tag created by scripts/build.sh --load.
# <image>/test/smoke.sh is called as: smoke.sh <image-ref>
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${1:-}"
[ -n "$image" ] && [ -x "$root/$image/test/smoke.sh" ] || { echo "usage: $0 <image> [image-ref]" >&2; exit 2; }
ref="${2:-malrunar/$image:h-$(python3 "$root/scripts/malrunar_image.py" hash "$image")}"
echo "smoke: $image -> $ref"
exec "$root/$image/test/smoke.sh" "$ref"
