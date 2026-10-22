#!/usr/bin/env bash
# Build one malrunar image with docker buildx.
#   scripts/build.sh <image> [--load] [--push] [-- extra buildx args]
# Reads pins.env (root) and <image>/pins.env and passes every pin as a --build-arg.
# The build context is the image directory. Tags: ${TAG_PREFIX:-malrunar}/<image>:<version> and :h-<hash>
# (publish.yml sets TAG_PREFIX=ghcr.io/iarnvidr).
# obs-runtime is built FROM desktop-base: DESKTOP_BASE is passed as a build arg and defaults to
# the local tag of the matching desktop-base build (build and --load that first), or set
# DESKTOP_BASE_REF=<registry ref or digest> to use another one.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool="$root/scripts/malrunar_image.py"

image="${1:-}"
[ -n "$image" ] || { sed -n '2,10p' "$0" >&2; exit 2; }
shift
mode=()
extra=()
while [ $# -gt 0 ]; do
    case "$1" in
        --load) mode+=(--load) ;;
        --push) mode+=(--push) ;;
        --) shift; extra=("$@"); break ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done
[ -d "$root/$image" ] || { echo "no such image: $image" >&2; exit 2; }

args=()
while IFS= read -r pin; do
    [ -n "$pin" ] && args+=(--build-arg "$pin")
done < <(python3 "$tool" pins "$image")

parent="$(python3 "$tool" parent "$image")"
if [ -n "$parent" ]; then
    ref="${DESKTOP_BASE_REF:-malrunar/$parent:h-$(python3 "$tool" hash "$parent")}"
    # obs-runtime has exactly one parent today (desktop-base); the arg name is DESKTOP_BASE.
    args+=(--build-arg "DESKTOP_BASE=$ref")
    echo "parent: $ref"
fi

tag_args=()
while IFS= read -r tag; do
    tag_args+=(--tag "${TAG_PREFIX:-malrunar}/$image:$tag")
done < <(python3 "$tool" tags "$image")

echo "building $image ($(python3 "$tool" hash "$image"))"
set -x
exec docker buildx build \
    --file "$root/$image/Dockerfile" \
    "${args[@]}" "${tag_args[@]}" \
    ${mode[@]+"${mode[@]}"} ${extra[@]+"${extra[@]}"} \
    "$root/$image"
