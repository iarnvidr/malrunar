#!/usr/bin/env bash
# CI helper: makes the three images the quickstart uses from this checkout and writes the
# MALRUNAR_*_IMAGE variables compose.yaml reads to the file named by $1 (default: $GITHUB_ENV).
# For each image the published one with the same content hash (tag h-<hash>) is used when it
# exists, so only what the pull request changed is built. Not part of the documented quickstart.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
out="${1:-${GITHUB_ENV:?pass a file to append to, or run in GitHub Actions}}"
tool=scripts/malrunar_image.py
registry="${QUICKSTART_REGISTRY:-ghcr.io/iarnvidr}"

# prepare <image> [parent ref]: prints the reference of the image to use.
prepare() {
    local image="$1" parent="${2:-}" hash ref
    hash="$(python3 "$tool" hash "$image")"
    ref="$registry/$image:h-$hash"
    if timeout -k 10 600 docker pull --quiet "$ref" >/dev/null 2>&1; then
        echo "using published $ref" >&2
    else
        echo "$ref is not published; building $image from this checkout" >&2
        PARENT_REF="$parent" timeout -k 30 3300 scripts/build.sh "$image" --load >&2
        ref="malrunar/$image:h-$hash"
    fi
    echo "$ref"
}

desktop_base="$(prepare desktop-base)"
export DESKTOP_BASE_REF="$desktop_base"
obs_runtime="$(prepare obs-runtime "$desktop_base")"
obs_control="$(prepare obs-control "$obs_runtime")"
media_bridge="$(prepare media-bridge)"

{
    echo "MALRUNAR_DESKTOP_BASE_IMAGE=$desktop_base"
    echo "MALRUNAR_MEDIA_BRIDGE_IMAGE=$media_bridge"
    echo "MALRUNAR_OBS_CONTROL_IMAGE=$obs_control"
} >> "$out"
cat "$out"
