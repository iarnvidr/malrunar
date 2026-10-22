#!/usr/bin/env bash
# Deliver a locally built Docker image to a remote Docker daemon over SSH.
#
#   tools/deliver_image.sh [options] IMAGE[:TAG] [USER@]HOST[:PORT]
#
# Methods (--method, or DELIVER_IMAGE_METHOD; default "pussh"):
#   pussh      unregistry's `docker pussh` (https://github.com/psviderski/unregistry).
#              Layers the target already has are skipped; only missing ones travel.
#              Needs the containerd image store on the target
#              (`docker info` reports driver-type io.containerd.snapshotter.v1);
#              fails clearly when it is not.
#   save-load  `docker save | gzip -1 | ssh ... docker load`: the whole image,
#              every time. Works with any store.
#   auto       pussh when the target uses the containerd store, else save-load.
#
# The `docker-pussh` script is fetched from a pinned commit and verified against
# a pinned sha256 before it runs, and the unregistry image it starts on the
# target is pinned by digest. Bump the four PINNED_* values together.
#
# SSH is whatever the caller has configured (ssh-agent, ~/.ssh/config with a
# ProxyCommand, ...). Optional overrides:
#   --ssh-config FILE   use this ssh_config instead of the default one
#   --known-hosts FILE  trust only the host keys in FILE (StrictHostKeyChecking yes)
#   --ssh-key FILE      private key for the target
# After delivery the image ID on the target must equal the local one.
# The registry, host and image are arguments: nothing here names a deployment.
# bash, curl and coreutils only (no Python, no PyYAML).
set -euo pipefail

PINNED_PUSSH_VERSION=0.4.3
PINNED_PUSSH_COMMIT=75f5b35683980a7cb3811645ef7ee0cfe3d64c00
PINNED_PUSSH_SHA256=3d97722b5f2804157f5ed65e7f2fa20695700c15a667b127d128f60a7527d5c8
PINNED_UNREGISTRY_IMAGE=ghcr.io/psviderski/unregistry@sha256:7297f3bb6f503515991e81e249dab81c23d2c350323fc209e3eaa8507a5970b1

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
die() { echo "deliver_image: $*" >&2; exit 1; }

method=${DELIVER_IMAGE_METHOD:-pussh}
ssh_config=
known_hosts=
ssh_key=
positional=()
while (($#)); do
    case $1 in
        --method) method=${2:?--method needs a value}; shift 2 ;;
        --method=*) method=${1#*=}; shift ;;
        --ssh-config) ssh_config=${2:?}; shift 2 ;;
        --known-hosts) known_hosts=${2:?}; shift 2 ;;
        --ssh-key) ssh_key=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) positional+=("$1"); shift ;;
    esac
done
((${#positional[@]} == 2)) || { usage >&2; exit 2; }
image=${positional[0]}
target=${positional[1]}
case $method in pussh|save-load|auto) ;; *) die "unknown method '$method' (pussh, save-load, auto)" ;; esac

work=$(mktemp -d "${TMPDIR:-/tmp}/deliver-image.XXXXXX")
trap 'rm -rf "$work"' EXIT

if [[ -n $known_hosts ]]; then
    [[ -s $known_hosts ]] || die "known_hosts file not found: $known_hosts"
    {
        if [[ -n $ssh_config ]]; then echo "Include $ssh_config"
        elif [[ -r $HOME/.ssh/config ]]; then echo "Include $HOME/.ssh/config"; fi
        printf 'Host *\n  UserKnownHostsFile %s\n  StrictHostKeyChecking yes\n' "$known_hosts"
    } > "$work/ssh_config"
    ssh_config=$work/ssh_config
fi
ssh_cmd=(ssh -o BatchMode=yes)
[[ -n $ssh_config ]] && ssh_cmd+=(-F "$ssh_config")
[[ -n $ssh_key ]] && ssh_cmd+=(-i "$ssh_key")

docker image inspect "$image" >/dev/null 2>&1 || die "image not found locally: $image"
local_id=$(docker image inspect --format '{{.Id}}' "$image")

remote_store=$("${ssh_cmd[@]}" "$target" "docker info --format '{{.DriverStatus}}'" 2>/dev/null) \
    || die "cannot run 'docker info' on $target over SSH"
if [[ $remote_store == *io.containerd.snapshotter* ]]; then containerd=1; else containerd=0; fi
echo "deliver_image: $target store: $([[ $containerd == 1 ]] && echo containerd || echo "not containerd ($remote_store)")"
if [[ $method == auto ]]; then
    if [[ $containerd == 1 ]]; then method=pussh; else method=save-load; fi
fi
if [[ $method == pussh && $containerd != 1 ]]; then
    die "docker pussh needs the containerd image store on $target (docker info lacks io.containerd.snapshotter.v1); use --method save-load or --method auto"
fi

# Approximate bytes sent: transmit counter of the default-route interface.
iface=$(awk '$2 == "00000000" {print $1; exit}' /proc/net/route 2>/dev/null || true)
tx_bytes() { [[ -n $iface && -r /sys/class/net/$iface/statistics/tx_bytes ]] && cat "/sys/class/net/$iface/statistics/tx_bytes" || echo 0; }
tx_before=$(tx_bytes)
started=$SECONDS

case $method in
    save-load)
        docker save "$image" | gzip -1 | "${ssh_cmd[@]}" "$target" 'gzip -d | docker load >/dev/null'
        ;;
    pussh)
        tools=${DELIVER_IMAGE_TOOLS_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/deliver-image-tools}
        mkdir -p "$tools"
        script=$tools/docker-pussh-$PINNED_PUSSH_VERSION
        if ! { [[ -f $script ]] && echo "$PINNED_PUSSH_SHA256  $script" | sha256sum -c - >/dev/null 2>&1; }; then
            curl -fsSL --retry 5 --retry-all-errors --connect-timeout 20 -o "$script.part" \
                "https://raw.githubusercontent.com/psviderski/unregistry/$PINNED_PUSSH_COMMIT/docker-pussh"
            echo "$PINNED_PUSSH_SHA256  $script.part" | sha256sum -c - >/dev/null \
                || die "docker-pussh $PINNED_PUSSH_VERSION failed its pinned checksum; refusing to run it"
            mv "$script.part" "$script"
        fi
        pussh_args=()
        [[ -n $ssh_config ]] && pussh_args+=(-F "$ssh_config")
        [[ -n $ssh_key ]] && pussh_args+=(-i "$ssh_key")
        export UNREGISTRY_IMAGE=${UNREGISTRY_IMAGE:-$PINNED_UNREGISTRY_IMAGE}
        bash "$script" ${pussh_args[@]+"${pussh_args[@]}"} "$image" "$target"
        ;;
esac

elapsed=$((SECONDS - started))
sent=$(($(tx_bytes) - tx_before))
remote_id=$("${ssh_cmd[@]}" "$target" "docker image inspect --format '{{.Id}}' '$image'") \
    || die "$image is not present on $target after delivery"
[[ $remote_id == "$local_id" ]] || die "image ID mismatch on $target: local $local_id, remote $remote_id"
summary="deliver_image: $image -> $target by $method in ${elapsed}s, node egress ~$((sent / 1048576)) MiB, image $local_id"
echo "$summary"
[[ -n ${GITHUB_STEP_SUMMARY:-} ]] && echo "$summary" >> "$GITHUB_STEP_SUMMARY" || true
