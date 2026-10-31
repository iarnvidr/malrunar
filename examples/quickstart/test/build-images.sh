#!/usr/bin/env bash
# Emit compose references for images already loaded by scripts/load_tested_images.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
manifest="${1:?pass the verified tested-image manifest}"
out="${2:-${GITHUB_ENV:?pass a GitHub environment file}}"

reference_for() {
    local image="$1"
    jq -er --arg image "$image" '.images[] | select(.image == $image) | .local_reference' "$manifest"
}

desktop_base="$(reference_for desktop-base)"
media_bridge="$(reference_for media-bridge)"
obs_control="$(reference_for obs-control)"
{
    printf 'MALRUNAR_DESKTOP_BASE_IMAGE=%s\n' "$desktop_base"
    printf 'MALRUNAR_MEDIA_BRIDGE_IMAGE=%s\n' "$media_bridge"
    printf 'MALRUNAR_OBS_CONTROL_IMAGE=%s\n' "$obs_control"
} >> "$out"
