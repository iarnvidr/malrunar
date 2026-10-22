# desktop-base: xrdp + xorgxrdp + XFCE + PipeWire/WirePlumber + ffmpeg + supervisor + tini on pinned
# Ubuntu 26.04, plus the UNMODIFIED official Firefox ESR and a compiled pipewire-module-xrdp.
#
# Generic by design. It creates no runtime user (the distro `ubuntu` user is removed so the first normal UID and
# GID are free for consumers), sets no HOME/DISPLAY/XDG_* environment, has no ENTRYPOINT or
# EXPOSE, and ships no session, PAM, xrdp or PipeWire configuration. Consumers derive from it
# (FROM) and add their own users, startwm.sh, sesman/PAM settings, audio graph and supervisord
# programs in private layers.
#
# Firefox: the official Mozilla linux-x86_64 en-US tarball, sha256-verified against the pin, unpacked
# as-is into /opt/malrunar/firefox. There is no policies.json, no distribution directory, no
# autoconfig, no extension and no omni.ja change, so the install stays unaltered under Mozilla's
# distribution policy. Consumers that need policies or extensions add them in their own private
# layers (for example /opt/malrunar/firefox/distribution/policies.json); doing so makes THEIR image a
# modified Firefox, which is their call. /usr/local/share/malrunar/desktop-base/firefox.* record the
# pristine file list and checksums so the smoke test (and a consumer) can compare. No symlink into
# PATH is created: call /opt/malrunar/firefox/firefox.
#
# Inputs, all pinned (see pins.env and the root pins.env): base image digest, apt snapshot date
# (https://snapshot.ubuntu.com), Firefox tarball sha256, pipewire-module-xrdp commit and tarball
# sha256. Build with scripts/build.sh desktop-base.
ARG UBUNTU_IMAGE

# --- pipewire-module-xrdp ----------------------------------------------------------------------
FROM ${UBUNTU_IMAGE} AS audio-build
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
ARG PIPEWIRE_MODULE_XRDP_COMMIT
ARG PIPEWIRE_MODULE_XRDP_SHA256
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt upgrade -y; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        ca-certificates curl gcc make autoconf automake libtool pkg-config autoconf-archive \
        libpipewire-0.3-dev libspa-0.2-dev; \
    malrunar_snapshot_disable
RUN curl -fsSL --retry 5 --retry-all-errors \
        "https://github.com/neutrinolabs/pipewire-module-xrdp/archive/${PIPEWIRE_MODULE_XRDP_COMMIT}.tar.gz" \
        -o /tmp/module.tar.gz; \
    echo "${PIPEWIRE_MODULE_XRDP_SHA256}  /tmp/module.tar.gz" | sha256sum -c -; \
    mkdir /tmp/module; \
    tar -xzf /tmp/module.tar.gz --strip-components=1 -C /tmp/module; \
    cd /tmp/module; \
    ./bootstrap; \
    ./configure; \
    make -j"$(nproc)"; \
    install -Dm755 src/.libs/libpipewire-module-xrdp.so \
        /out/usr/lib/x86_64-linux-gnu/pipewire-0.3/libpipewire-module-xrdp.so; \
    install -Dm644 LICENSE /out/usr/local/share/malrunar/licenses/pipewire-module-xrdp/LICENSE

# --- desktop-base ------------------------------------------------------------------------------
FROM ${UBUNTU_IMAGE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
ARG FIREFOX_ESR_VERSION
ARG FIREFOX_ESR_SHA256
ARG PIPEWIRE_MODULE_XRDP_COMMIT
ARG PIPEWIRE_MODULE_XRDP_SHA256
COPY scripts/apt-snapshot.sh /usr/local/share/malrunar/lib/apt-snapshot.sh
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt upgrade -y; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        ca-certificates curl dbus dbus-x11 ffmpeg fonts-dejavu fonts-liberation \
        libasound2t64 libdbus-glib-1-2 libgtk-3-0t64 libnss3 libxt6t64 libx11-xcb1 \
        pipewire pipewire-pulse wireplumber pulseaudio-utils python3 \
        supervisor tini x11-utils x11-xserver-utils xauth xorgxrdp xrdp \
        xfce4-session xfce4-panel xfce4-settings xfdesktop4 xfwm4 thunar \
        procps util-linux v4l-utils xz-utils zip; \
    malrunar_snapshot_disable; \
    if getent passwd ubuntu >/dev/null; then userdel -r ubuntu; fi; \
    if getent group ubuntu >/dev/null; then groupdel ubuntu; fi; \
    usermod -a -G ssl-cert xrdp
COPY --from=audio-build /out/ /
RUN mkdir -p /opt/malrunar /usr/local/share/malrunar/desktop-base; \
    curl -fsSL --retry 5 --retry-all-errors \
        "https://ftp.mozilla.org/pub/firefox/releases/${FIREFOX_ESR_VERSION}/linux-x86_64/en-US/firefox-${FIREFOX_ESR_VERSION}.tar.xz" \
        -o /tmp/firefox.tar.xz; \
    echo "${FIREFOX_ESR_SHA256}  /tmp/firefox.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/firefox.tar.xz -C /opt/malrunar; \
    rm /tmp/firefox.tar.xz; \
    test -x /opt/malrunar/firefox/firefox; \
    cd /opt/malrunar/firefox; \
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /usr/local/share/malrunar/desktop-base/firefox.sha256; \
    find . -mindepth 1 -printf '%y %m %u:%g %l %p\n' | LC_ALL=C sort -k5 > /usr/local/share/malrunar/desktop-base/firefox.tree; \
    printf '%s\n' \
        "APT_SNAPSHOT=${APT_SNAPSHOT}" \
        "FIREFOX_ESR_VERSION=${FIREFOX_ESR_VERSION}" \
        "FIREFOX_ESR_SHA256=${FIREFOX_ESR_SHA256}" \
        "PIPEWIRE_MODULE_XRDP_COMMIT=${PIPEWIRE_MODULE_XRDP_COMMIT}" \
        "PIPEWIRE_MODULE_XRDP_SHA256=${PIPEWIRE_MODULE_XRDP_SHA256}" \
        > /usr/local/share/malrunar/desktop-base/build-info.env
COPY share/wireplumber-virtual-only.conf.example /usr/local/share/malrunar/desktop-base/examples/wireplumber-virtual-only.conf.example
