#!/usr/bin/env bash
# Run the Docker tests of pr.yml on GitHub Actions, for people without a local Docker.
#
#   scripts/ci-try.sh <components|all> [--level unit|smoke|e2e|all] [--ref <branch>] [--keep]
#
#   components   comma-separated image names (obs-control,media-bridge), "changed" (what differs from
#                main) or "all". Test only what you changed: see "No local Docker?" in CONVENTIONS.md.
#   --level      unit  static checks and unit tests only (no image is built)
#                smoke build the images and run their smoke tests
#                e2e   the end-to-end test only (obs-control -> media-bridge)
#                all   everything for those components (default)
#   --ref        test this branch of origin as it is (it must contain the pr.yml with the dispatch
#                inputs) instead of pushing HEAD to a scratch branch. It is never deleted.
#   --keep       keep the scratch branch ci-try/<who>-<shortsha> (default: deleted at the end)
#
# What it does: pushes HEAD to the scratch branch, dispatches pr.yml (workflow_dispatch) there, finds
# the run it started (head sha + created after the dispatch, never "the latest run"), waits for it,
# prints the failing step and the end of the log of every failed job, downloads the artifacts of that
# run to <repo>/.ci-try/<run-id>/ and deletes the scratch branch. Uncommitted changes are NOT tested.
#
# Exit status: 0 the run passed, 1 a test failed, 2 usage or infrastructure error (no run, cancelled,
# timeout, gh or git failed). Needs bash and an authenticated gh (gh auth login) that may push to origin.
#
# Environment: CI_TRY_TIMEOUT seconds to wait (default 5400; the run is cancelled when it expires),
# CI_TRY_TAIL log lines per failed job (default 80), CI_TRY_NAME the name used in the branch name
# (default: git user.name), CI_TRY_REPO owner/name (default: the repository of origin).
set -euo pipefail

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2; }
die() { echo "ci-try: $*" >&2; exit 2; }

components=''
level=all
ref=''
keep=0
while [ $# -gt 0 ]; do
    case "$1" in
        --level) [ $# -ge 2 ] || die "--level needs a value"; level="$2"; shift ;;
        --ref) [ $# -ge 2 ] || die "--ref needs a branch"; ref="$2"; shift ;;
        --keep) keep=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage; die "unknown option $1" ;;
        *) [ -z "$components" ] || { usage; die "components given twice"; }; components="$1" ;;
    esac
    shift
done
[ -n "$components" ] || { usage; die "components is required (names, changed or all)"; }
case "$level" in unit|smoke|e2e|all) ;; *) die "--level must be unit, smoke, e2e or all (got $level)" ;; esac
components="${components// /,}"

command -v gh >/dev/null || die "gh is not installed"
root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run this inside a clone of the repository"
cd "$root"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
repo="${CI_TRY_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)}" || die "cannot determine the repository"
[ -n "$repo" ] || die "cannot determine the repository"

# Validate component names against the registry of images, so a typo fails here and not after a push.
if [ "$components" != all ] && [ "$components" != changed ]; then
    known="$(python3 scripts/malrunar_image.py list)" || die "cannot list the images"
    IFS=',' read -r -a wanted <<< "$components"
    for name in "${wanted[@]}"; do
        printf '%s\n' "$known" | grep -qx -- "$name" || die "unknown component '$name'; known: $(echo "$known" | tr '\n' ' ')"
    done
fi

timeout_s="${CI_TRY_TIMEOUT:-5400}"
tail_n="${CI_TRY_TAIL:-80}"
esc="$(printf '\033')"

scratch=''        # scratch branch created by this run (deleted at the end unless --keep)
run_id=''
finished=0
status=2
workdir="$(mktemp -d)"

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -n "$run_id" ] && [ "$finished" -eq 0 ]; then
        echo "ci-try: stopping run $run_id" >&2
        gh run cancel "$run_id" -R "$repo" >/dev/null 2>&1 || true
    fi
    if [ -n "$scratch" ]; then
        if [ "$keep" -eq 1 ]; then
            echo "ci-try: kept scratch branch $scratch (delete it: git push origin --delete $scratch)"
        elif git push --quiet origin --delete "$scratch" 2>/dev/null; then
            echo "ci-try: deleted scratch branch $scratch"
        else
            echo "ci-try: could not delete scratch branch $scratch (git push origin --delete $scratch)" >&2
        fi
    fi
    rm -rf "$workdir"
    [ "$rc" -eq 0 ] || [ "$status" -ne 2 ] || status=2
    exit "$status"
}
trap cleanup EXIT
trap 'echo "ci-try: interrupted" >&2; status=2; exit 2' INT TERM

if [ -n "$ref" ]; then
    branch="$ref"
    sha="$(gh api "repos/$repo/commits/$ref" --jq .sha 2>/dev/null)" || die "branch '$ref' does not exist on $repo (push it first)"
else
    sha="$(git rev-parse HEAD)"
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "ci-try: warning: uncommitted changes are not tested (commit them first)" >&2
    fi
    who="${CI_TRY_NAME:-$(git config user.name 2>/dev/null || true)}"
    who="$(printf '%s' "${who:-${USER:-agent}}" | tr -c 'A-Za-z0-9\n' '-' | sed -E 's/-+/-/g; s/^-//; s/-$//' | cut -c1-30)"
    branch="ci-try/${who:-agent}-${sha:0:7}"
    scratch="$branch"
    git push --quiet --force origin "HEAD:refs/heads/$branch" || { scratch=''; die "cannot push $branch to origin"; }
fi
echo "ci-try: repository $repo, branch $branch, commit ${sha:0:12}, components $components, level $level"

# GitHub's own clock (Date header), so a skewed local clock cannot hide or fake the run.
since="$(gh api -i rate_limit 2>/dev/null | sed -n 's/^[Dd]ate: *//p' | head -n 1 | tr -d '\r')"
if [ -n "$since" ]; then since="$(date -u -d "$since" +%Y-%m-%dT%H:%M:%SZ)"; else since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; fi

if ! gh workflow run pr.yml -R "$repo" --ref "$branch" -f "components=$components" -f "level=$level" >"$workdir/dispatch.log" 2>&1; then
    cat "$workdir/dispatch.log" >&2
    die "dispatch failed: $branch must contain a pr.yml with the workflow_dispatch inputs components and level"
fi

echo "ci-try: dispatched, looking for the run (head sha $sha, created at or after $since)"
for _ in $(seq 1 30); do
    run_id="$(gh run list -R "$repo" --workflow pr.yml --branch "$branch" --limit 30 \
        --json databaseId,headSha,createdAt,event --jq "[.[] | select(.event == \"workflow_dispatch\" and .headSha == \"$sha\" and .createdAt >= \"$since\")] | sort_by(.createdAt) | .[0].databaseId // empty" 2>"$workdir/list.log")" || { cat "$workdir/list.log" >&2; die "cannot list runs"; }
    [ -n "$run_id" ] && break
    sleep 3
done
[ -n "$run_id" ] || die "the dispatched run did not appear within 90 s"
url="https://github.com/$repo/actions/runs/$run_id"
echo "ci-try: run $run_id $url"

echo "ci-try: waiting (at most $timeout_s s)"
watch_rc=0
timeout "$timeout_s" gh run watch "$run_id" -R "$repo" --exit-status --interval 30 >"$workdir/watch.log" 2>&1 || watch_rc=$?
tail -n 25 "$workdir/watch.log" | sed "s/${esc}\\[[0-9;?]*[A-Za-z]//g"
if [ "$watch_rc" -eq 124 ]; then
    echo "ci-try: timed out after $timeout_s s; cancelling the run" >&2
    gh run cancel "$run_id" -R "$repo" >/dev/null 2>&1 || true
    finished=1
    die "timeout, see $url"
fi

conclusion="$(gh run view "$run_id" -R "$repo" --json conclusion --jq .conclusion)" || die "cannot read the result of run $run_id"
[ -n "$conclusion" ] || die "the run has no result yet (gh run watch exited with $watch_rc); it is cancelled now"
finished=1
echo "ci-try: conclusion: $conclusion ($url)"

# The artifacts of the run (the jobs upload container logs, smoke/e2e output and image digests when they fail).
art="$root/.ci-try/$run_id"
mkdir -p "$art"
if gh run download "$run_id" -R "$repo" --dir "$art" >"$workdir/download.log" 2>&1; then
    echo "ci-try: artifacts in $art:"
    find "$art" -type f | sort | sed 's/^/  /'
else
    echo "ci-try: no artifacts for this run ($(tail -n 1 "$workdir/download.log"))"
    rmdir "$art" 2>/dev/null || true
fi

case "$conclusion" in
    success) status=0 ;;
    failure)
        status=1
        gh run view "$run_id" -R "$repo" --json jobs --jq '.jobs[] | select(.conclusion == "failure") | [(.databaseId | tostring), .name, ([.steps[] | select(.conclusion == "failure") | .name] | join("; "))] | @tsv' |
        while IFS="$(printf '\t')" read -r job_id job_name step_name; do
            echo
            echo "=== FAILED job: $job_name"
            echo "=== failing step: ${step_name:-(none recorded)}"
            echo "=== last $tail_n relevant log lines:"
            gh run view --job "$job_id" -R "$repo" --log-failed 2>&1 |
                sed "s/${esc}\\[[0-9;?]*[A-Za-z]//g" | cut -f3- |
                sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z ?//' |
                grep -vE '^(##\[(debug|group|endgroup)\]|\s*$|frame= *[0-9]+ fps=)' | tail -n "$tail_n" || true
        done
        ;;
    *) status=2; echo "ci-try: the run ended as '$conclusion' (cancelled or infrastructure problem), not as a test failure" >&2 ;;
esac
exit "$status"
