#!/usr/bin/env bash
# Build one malrunar image with docker buildx.
#   scripts/build.sh <image> [--load] [--push] [-- extra buildx args]
# Reads pins.env (root) and <image>/pins.env and passes every pin as a --build-arg.
# The build context is the image directory. Tags: ${TAG_PREFIX:-malrunar}/<image>:<version> and :h-<hash>
# (publish.yml sets TAG_PREFIX=ghcr.io/iarnvidr).
# An image built FROM another one of ours (obs-runtime FROM desktop-base, obs-control FROM
# obs-runtime) receives its parent as a build arg named after the parent (DESKTOP_BASE, OBS_RUNTIME).
# It defaults to the local tag of the matching parent build (build and --load that first), or set
# PARENT_REF=<registry ref or digest> to use another one (DESKTOP_BASE_REF is still read when the
# parent is desktop-base).
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
    parent_arg="$(printf '%s' "$parent" | tr 'a-z-' 'A-Z_')"
    ref="${PARENT_REF:-}"
    if [ -z "$ref" ] && [ "$parent" = desktop-base ]; then ref="${DESKTOP_BASE_REF:-}"; fi
    : "${ref:=malrunar/$parent:h-$(python3 "$tool" hash "$parent")}"
    args+=(--build-arg "$parent_arg=$ref")
    echo "parent: $ref ($parent_arg)"
fi

tag_args=()
while IFS= read -r tag; do
    tag_args+=(--tag "${TAG_PREFIX:-malrunar}/$image:$tag")
done < <(python3 "$tool" tags "$image")

echo "building $image ($(python3 "$tool" hash "$image"))"
set -x
candidate=false
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ] && [ -n "${MALRUNAR_CANDIDATE_DIR:-}" ] \
    && [[ " ${MALRUNAR_CANDIDATE_IMAGES:-} " == *" $image "* ]]; then
    for option in "${mode[@]}"; do
        [ "$option" = --load ] && candidate=true
        [ "$option" = --push ] && candidate=false
    done
fi

if [ "$candidate" = true ]; then
    docker buildx build \
        --file "$root/$image/Dockerfile" \
        "${args[@]}" "${tag_args[@]}" \
        "${mode[@]}" ${extra[@]+"${extra[@]}"} \
        "$root/$image"
    hash="$(python3 "$tool" hash "$image")"
    mkdir -p "$MALRUNAR_CANDIDATE_DIR"
    docker buildx build \
        --platform linux/amd64 \
        --file "$root/$image/Dockerfile" \
        "${args[@]}" \
        --output "type=oci,dest=$MALRUNAR_CANDIDATE_DIR/$image.oci.tar,name=malrunar/$image:h-$hash,oci-mediatypes=true" \
        --sbom=true --provenance=mode=max \
        ${extra[@]+"${extra[@]}"} \
        "$root/$image"
else
    exec docker buildx build \
        --file "$root/$image/Dockerfile" \
        "${args[@]}" "${tag_args[@]}" \
        ${mode[@]+"${mode[@]}"} ${extra[@]+"${extra[@]}"} \
        "$root/$image"
fi
