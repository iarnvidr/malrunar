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
# In CI (MALRUNAR_CI_LOG_DIR set) keep a copy of the output: it is uploaded as an artifact when the job fails.
if [ -n "${MALRUNAR_CI_LOG_DIR:-}" ]; then
    mkdir -p "$MALRUNAR_CI_LOG_DIR"
    exec > >(tee -a "$MALRUNAR_CI_LOG_DIR/smoke-$image.log") 2>&1
fi
echo "smoke: $image -> $ref"
exec "$root/$image/test/smoke.sh" "$ref"
