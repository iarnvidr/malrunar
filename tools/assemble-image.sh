#!/usr/bin/env bash
# Assemble an image from a base image and component images that are already in this Docker
# daemon's store, check it, and only then tag it.
#
#   tools/assemble-image.sh --dockerfile FILE --tag IMAGE[:TAG]... [--checks SCRIPT] ARG=image-ref...
#
#   --dockerfile FILE   the assembly Dockerfile: base plus `COPY --link --from=<component> / /` lines,
#                       generated from the manifest by
#                       `scripts/malrunar_image.py graph MANIFEST assemble-dockerfile`
#   --tag IMAGE[:TAG]   the name the finished image gets, and only when the checks pass (repeatable)
#   --checks SCRIPT     a bash script run as root inside the candidate image (stdin of
#                       `docker run --entrypoint bash IMAGE -s`); the assembly fails when it fails
#   ARG=image-ref       one per Dockerfile ARG, as printed by `... graph MANIFEST assemble-args`
#
# Runs on the machine that holds the images: the deploy host (after tools/deliver_image.sh sent them)
# or the build machine. The candidate is built with `COPY --link` only, so an unchanged component
# is a byte-identical layer, and the checks run in it every time, because run-time coupling between
# the base and a component is not visible in any component hash. The previous image under --tag stays
# untouched when anything fails. Every container run here is bounded by `timeout`
# (ASSEMBLE_CHECK_TIMEOUT seconds, default 900).
set -euo pipefail

die() { echo "assemble-image: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

dockerfile=
tags=()
checks=
refs=()
while (($#)); do
    case $1 in
        --dockerfile) dockerfile=${2:?--dockerfile needs a value}; shift 2 ;;
        --tag) tags+=("${2:?--tag needs a value}"); shift 2 ;;
        --checks) checks=${2:?--checks needs a value}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) refs+=("$1"); shift ;;
    esac
done
[[ -n $dockerfile && ${#tags[@]} -gt 0 ]] || { usage >&2; exit 2; }
[[ -f $dockerfile ]] || die "no such Dockerfile: $dockerfile"
[[ -z $checks || -f $checks ]] || die "no such checks script: $checks"
((${#refs[@]} > 0)) || die 'no ARG=image-ref arguments'

build_args=()
components=
for item in "${refs[@]}"; do
    [[ $item =~ ^[A-Z][A-Z0-9_]*=[^[:space:]]+$ ]] || die "bad ARG=image-ref argument: $item"
    ref=${item#*=}
    docker image inspect "$ref" >/dev/null 2>&1 || die "$ref is not in this Docker store"
    build_args+=(--build-arg "$item")
    components+="${components:+,}$item"
done

candidate=malrunar-assemble:candidate-$$
context=$(mktemp -d)
cleanup() { rmdir "$context" 2>/dev/null || true; docker rmi "$candidate" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# The context is empty: nothing is copied from it.
docker build --progress=plain --target final -f "$dockerfile" "${build_args[@]}" \
    --build-arg "ASSEMBLE_COMPONENTS=$components" -t "$candidate" "$context"
echo "assembled $candidate: $(docker image inspect --format '{{.Id}} {{len .RootFS.Layers}} layers' "$candidate")"
if [[ -n $checks ]]; then
    timeout -k 10 "${ASSEMBLE_CHECK_TIMEOUT:-900}" \
        docker run --rm -i --user root --entrypoint bash "$candidate" -s < "$checks" \
        || die "the checks failed: ${tags[*]} not moved"
    echo "checks passed: $checks"
fi
for tag in "${tags[@]}"; do
    docker tag "$candidate" "$tag"
    echo "tagged $tag"
done
