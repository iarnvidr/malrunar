#!/usr/bin/env bash
# Starts the media-bridge as the user "desktop". When a camera device is mapped into the container
# (compose.camera.yaml), first lets that user open it: the device belongs to the host's "video"
# group, whose number differs between hosts.
set -euo pipefail
user=desktop
device="${MALRUNAR_BRIDGE_VIDEO_DEVICE:-/dev/video10}"

if [[ "${MALRUNAR_BRIDGE_VIDEO_SINK:-v4l2}" == v4l2 && -c "$device" ]]; then
    gid="$(stat -c %g "$device")"
    if [[ "$gid" == 0 ]]; then
        echo "bridge-start: $device belongs to group root; run the host setup (media-bridge/host/setup-v4l2loopback.sh) or give the device to a group such as video" >&2
    else
        group="$(getent group "$gid" | cut -d: -f1 || true)"
        if [[ -z "$group" ]]; then
            group="hostvideo$gid"
            groupadd --gid "$gid" "$group"
        fi
        usermod --append --groups "$group" "$user"
    fi
fi

# runuser reads the user's groups from /etc/group, so the group added above applies.
exec runuser -u "$user" -- /opt/malrunar/media-bridge/bin/malrunar-media-bridge
