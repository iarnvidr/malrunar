#!/usr/bin/env bash
# Helper to be sourced (bash) by RUN steps: apt against a pinned snapshot.ubuntu.com date, over TLS.
#
#   . /usr/local/share/malrunar/lib/apt-snapshot.sh      # in desktop-base images
#   malrunar_snapshot_enable      # needs APT_SNAPSHOT=<YYYYMMDDTHHMMSSZ>
#   malrunar_retry malrunar_apt install -y --no-install-recommends <packages>
#   malrunar_snapshot_disable     # puts the image's own apt sources back and drops the lists
#
# The Ubuntu base image has no CA bundle and snapshot.ubuntu.com redirects http to https, so
# `enable` first installs ca-certificates from the image's stock archive sources (plain http; apt
# verifies those indexes with the Ubuntu archive keyring), then switches to
# https://snapshot.ubuntu.com. Certificate and signature verification are never disabled.
# ca-certificates comes from the live archive on that first install and from the snapshot after the
# next `apt-get upgrade` or install of it.

malrunar_retry() {
    local n=1
    until "$@"; do
        [ "$n" -lt 5 ] || return 1
        sleep $((n * 15))
        n=$((n + 1))
    done
}

malrunar_apt() {
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"
}

malrunar_snapshot_enable() {
    [ -n "${APT_SNAPSHOT:-}" ] || { echo 'APT_SNAPSHOT is not set' >&2; return 1; }
    local VERSION_CODENAME
    VERSION_CODENAME="$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")"
    if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
        malrunar_retry malrunar_apt update
        malrunar_retry malrunar_apt install -y --no-install-recommends ca-certificates
    fi
    mkdir -p /var/tmp/malrunar-apt-orig
    if [ ! -e /var/tmp/malrunar-apt-orig/sources.list.d ]; then
        cp -a /etc/apt/sources.list.d /var/tmp/malrunar-apt-orig/sources.list.d
        if [ -e /etc/apt/sources.list ]; then cp -a /etc/apt/sources.list /var/tmp/malrunar-apt-orig/sources.list; fi
    fi
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
    printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s/\nSuites: %s %s-updates %s-security\nComponents: main universe restricted multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        "$APT_SNAPSHOT" "$VERSION_CODENAME" "$VERSION_CODENAME" "$VERSION_CODENAME" \
        > /etc/apt/sources.list.d/ubuntu.sources
    malrunar_retry malrunar_apt update
}

malrunar_snapshot_disable() {
    rm -rf /var/lib/apt/lists/*
    if [ -d /var/tmp/malrunar-apt-orig/sources.list.d ]; then
        rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
        cp -a /var/tmp/malrunar-apt-orig/sources.list.d/. /etc/apt/sources.list.d/
        if [ -e /var/tmp/malrunar-apt-orig/sources.list ]; then cp -a /var/tmp/malrunar-apt-orig/sources.list /etc/apt/sources.list; fi
        rm -rf /var/tmp/malrunar-apt-orig
    fi
}
