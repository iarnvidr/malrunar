#!/usr/bin/env bash
# guacd's apt source, on top of the shared snapshot helper (apt-snapshot.sh, identical in every image).
#
#   . /scripts/guacd-apt.sh
#   guacd_apt_enable      # GUACD_APT_SNAPSHOT=<YYYYMMDDTHHMMSSZ>: that dated snapshot; empty: the live Ubuntu archive
#   malrunar_retry malrunar_apt install -y --no-install-recommends <packages>
#   guacd_apt_disable
#
# guacd records the exact libfreerdp/winpr package versions it was linked against, and a consumer that installs the
# same libraries itself (the RK workstation) must carry the very same versions. Both therefore have to come from the
# same apt source: while the dated snapshot service is unavailable, guacd and its consumers use the live archive and the
# recorded package list plus the image digest and SBOM are the evidence; a date makes the build reproducible again.
# Index signatures and certificates are verified in both modes.
. /scripts/apt-snapshot.sh

guacd_apt_enable() {
    if [ -n "${GUACD_APT_SNAPSHOT:-}" ]; then
        export APT_SNAPSHOT="$GUACD_APT_SNAPSHOT"
        malrunar_snapshot_enable
    else
        malrunar_retry malrunar_apt update
    fi
}

guacd_apt_disable() {
    if [ -n "${GUACD_APT_SNAPSHOT:-}" ]; then
        malrunar_snapshot_disable
    else
        rm -rf /var/lib/apt/lists/*
    fi
}
