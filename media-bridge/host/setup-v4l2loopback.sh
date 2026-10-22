#!/usr/bin/env bash
# Copyright 2026 efthomsen
# SPDX-License-Identifier: Apache-2.0
#
# setup-v4l2loopback.sh: create virtual camera devices on the HOST with the v4l2loopback kernel
# module, so a container running malrunar-media-bridge can write to them (docker run --device).
# A container cannot load kernel modules; this is a host (VM, workstation, node) concern.
#
#   setup-v4l2loopback.sh [options]
#     -n, --devices N        number of devices (default 1)
#     -f, --first-nr NR      number of the first device: /dev/video<NR>, NR+1, ... (default 10)
#     -l, --label TEXT       camera name shown to applications; repeat once per device, or give
#                            one to name them "TEXT 1", "TEXT 2", ... (default "Virtual Camera")
#         --persist          also load the module at boot (/etc/modules-load.d and /etc/modprobe.d)
#         --dry-run          print what would be done, change nothing
#     -h, --help
#
# exclusive_caps=1 is always set: browsers and call apps only list a loopback device as a camera
# when it advertises capture capability alone, which exclusive_caps=1 makes it do.
#
# Idempotent: if the devices already exist with the requested numbers and labels, nothing is done.
# If the module is loaded with a different layout it is reloaded, but only when no device is in
# use; the script refuses (exit 1) rather than interrupt an application with a camera open.
# Needs root (or sudo) except with --dry-run, and the v4l2loopback module for the running kernel
# (Ubuntu/Debian: apt install v4l2loopback-dkms, with the matching linux-headers package).
set -euo pipefail

prog="setup-v4l2loopback"
die() { printf '%s: %s\n' "$prog" "$*" >&2; exit 1; }
sysfs="${MALRUNAR_SYSFS:-/sys}"        # overridable for tests
devdir="${MALRUNAR_DEVDIR:-/dev}"      # overridable for tests
confdir="${MALRUNAR_CONFDIR:-/etc}"    # overridable for tests

count=1
first=10
labels=()
persist=0
dry=0
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--devices) count="${2:?$1 needs a value}"; shift 2 ;;
        -f|--first-nr) first="${2:?$1 needs a value}"; shift 2 ;;
        -l|--label) labels+=("${2:?$1 needs a value}"); shift 2 ;;
        --persist) persist=1; shift ;;
        --dry-run) dry=1; shift ;;
        -h|--help) sed -n '5,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' ; exit 0 ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
done

[[ "$count" =~ ^[1-9][0-9]*$ ]] && (( count <= 32 )) || die "--devices must be 1-32, got '$count'"
[[ "$first" =~ ^[0-9]+$ ]] || die "--first-nr must be a number, got '$first'"
if [ "${#labels[@]}" -eq 0 ]; then labels=("Virtual Camera"); fi
if [ "${#labels[@]}" -eq 1 ] && (( count > 1 )); then
    base="${labels[0]}"; labels=()
    for (( i = 1; i <= count; i++ )); do labels+=("$base $i"); done
fi
[ "${#labels[@]}" -eq "$count" ] || die "got ${#labels[@]} labels for $count devices"
for label in "${labels[@]}"; do
    [[ "$label" =~ ^[A-Za-z0-9\ ._-]{1,31}$ ]] || die "label '$label' must be 1-31 characters of letters, digits, space, . _ - (the kernel limit is 31)"
done

numbers=(); exclusive=()
for (( i = 0; i < count; i++ )); do numbers+=("$(( first + i ))"); exclusive+=(1); done
join() { local IFS=,; printf '%s' "$*"; }
nr_list="$(join "${numbers[@]}")"
caps_list="$(join "${exclusive[@]}")"
label_list="$(join "${labels[@]}")"
options="devices=$count video_nr=$nr_list card_label=$label_list exclusive_caps=$caps_list"
quoted_label_list=""
for label in "${labels[@]}"; do quoted_label_list+="${quoted_label_list:+,}\"$label\""; done
persist_options="options v4l2loopback devices=$count video_nr=$nr_list card_label=$quoted_label_list exclusive_caps=$caps_list"

run() {
    if (( dry )); then printf 'dry-run: %s\n' "$*"; return 0; fi
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

# Does device number $1 exist with the name $2?
device_matches() {
    local name_file="$sysfs/class/video4linux/video$1/name"
    [ -r "$name_file" ] && [ "$(cat "$name_file")" = "$2" ]
}

all_match=1
for (( i = 0; i < count; i++ )); do
    device_matches "${numbers[$i]}" "${labels[$i]}" || all_match=0
done

module_loaded=0
[ -d "$sysfs/module/v4l2loopback" ] && module_loaded=1

if (( all_match )); then
    echo "$prog: devices already present: $(for (( i = 0; i < count; i++ )); do printf '%s/video%s="%s" ' "$devdir" "${numbers[$i]}" "${labels[$i]}"; done)"
else
    if (( module_loaded )); then
        # The module's use count is the number of open loopback devices (readers and writers).
        refcnt="$(cat "$sysfs/module/v4l2loopback/refcnt" 2>/dev/null || echo 0)"
        in_use=""
        if (( refcnt > 0 )); then in_use="module use count is $refcnt"; fi
        if command -v fuser >/dev/null; then
            for node in "$devdir"/video*; do
                [ -c "$node" ] || continue
                nr="${node##*video}"
                [ -r "$sysfs/class/video4linux/video$nr/name" ] || continue
                if fuser "$node" >/dev/null 2>&1; then in_use+="${in_use:+; }$node is open"; fi
            done
        fi
        if [ -n "$in_use" ]; then
            die "v4l2loopback is loaded with a different layout and is in use ($in_use); refusing to unload it. Stop the programs using the cameras and run this again."
        fi
        echo "$prog: v4l2loopback is loaded with a different layout and idle; reloading it"
        run modprobe -r v4l2loopback
    fi
    run modprobe v4l2loopback devices="$count" video_nr="$nr_list" card_label="$label_list" exclusive_caps="$caps_list" \
        || die "modprobe v4l2loopback failed. Is the module installed for this kernel (apt install v4l2loopback-dkms linux-headers-\$(uname -r))? Is Secure Boot refusing an unsigned module?"
    if (( ! dry )); then
        for (( i = 0; i < count; i++ )); do
            device_matches "${numbers[$i]}" "${labels[$i]}" \
                || die "after loading the module $devdir/video${numbers[$i]} is not the expected camera '${labels[$i]}'; the number may be taken by another video device"
        done
        echo "$prog: created: $options"
    fi
fi

if (( persist )); then
    write_if_changed() {   # $1 file, $2 content
        if [ -r "$1" ] && [ "$(cat "$1")" = "$2" ]; then echo "$prog: $1 is up to date"; return 0; fi
        if (( dry )); then printf 'dry-run: write %s: %s\n' "$1" "$2"; return 0; fi
        run mkdir -p "$(dirname -- "$1")"
        printf '%s\n' "$2" | run tee "$1" >/dev/null
        echo "$prog: wrote $1"
    }
    write_if_changed "$confdir/modules-load.d/malrunar-media-bridge.conf" v4l2loopback
    write_if_changed "$confdir/modprobe.d/malrunar-media-bridge.conf" "$persist_options"
fi
