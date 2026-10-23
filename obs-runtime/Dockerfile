# obs-runtime: OBS Studio (with obs-browser/CEF) and the libraries and helpers it needs, on top of
# desktop-base. One chain: desktop-base -> obs-runtime.
#
# OBS comes from the OBS Project PPA, but is not installed through the PPA: the exact .deb is
# downloaded by URL, verified against the sha256 in pins.env and installed with `apt install ./obs.deb`,
# so no PPA key lookup, no add-apt-repository and no TLS-verification flags are involved. The deb's
# Depends (Qt6, ffmpeg, libpython, CEF's NSS/CUPS/ATK/X libraries, ...) resolve from the pinned Ubuntu
# snapshot (https://snapshot.ubuntu.com, same date as desktop-base). The deb and its source package
# are mirrored as release assets by obs-runtime/fetch-source.sh (GPL source offer), because the PPA
# drops superseded versions.
#
# The OBS .deb bundles CEF (Chromium Embedded Framework) as obs-plugins/libcef.so: see cef-version.env
# (installed under /usr/local/share/malrunar/obs-runtime/) and the CEF licence under
# /usr/local/share/malrunar/licenses/cef/. /usr/share/doc/*/copyright files are kept.
#
# Generic by design: no user, no HOME/DISPLAY/XDG_* environment, no ENTRYPOINT, no OBS profile or
# scene configuration. Consumers add their users, X display setup, scenes and scripts (and any Python
# environment) in their own layers. OBS lives at /usr/bin/obs, plugins in
# /usr/lib/x86_64-linux-gnu/obs-plugins (the .deb's own layout).
ARG DESKTOP_BASE
FROM ${DESKTOP_BASE} AS runtime-base
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
ARG OBS_VERSION
ARG OBS_DEB_SHA256
ARG OBS_PPA_URL
COPY licenses/cef-LICENSE.txt /usr/local/share/malrunar/licenses/cef/LICENSE.txt
COPY cef-version.env /usr/local/share/malrunar/obs-runtime/cef-version.env
# Launchpad fetches time out intermittently from CI, so every network step retries.
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt upgrade -y; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        xvfb openbox pulseaudio libgl1-mesa-dri mesa-libgallium; \
    malrunar_retry curl -fsSL --connect-timeout 30 \
        "${OBS_PPA_URL}/+files/obs-studio_${OBS_VERSION}_amd64.deb" -o /tmp/obs-studio.deb; \
    echo "${OBS_DEB_SHA256}  /tmp/obs-studio.deb" | sha256sum -c -; \
    chmod 644 /tmp/obs-studio.deb; \
    malrunar_retry malrunar_apt install -y --no-install-recommends /tmp/obs-studio.deb; \
    rm -f /tmp/obs-studio.deb; \
    malrunar_snapshot_disable; \
    test "$(dpkg-query -W -f='${Version}' obs-studio)" = "${OBS_VERSION}"; \
    test -r /usr/lib/x86_64-linux-gnu/obs-plugins/obs-browser.so; \
    test -r /usr/lib/x86_64-linux-gnu/obs-plugins/libcef.so; \
    for f in obs-browser.so libcef.so obs-browser-page; do \
        if ldd "/usr/lib/x86_64-linux-gnu/obs-plugins/$f" | grep -q 'not found'; then \
            ldd "/usr/lib/x86_64-linux-gnu/obs-plugins/$f"; exit 1; \
        fi; \
    done; \
    mkdir -p /usr/local/share/malrunar/obs-runtime; \
    printf '%s\n' "OBS_VERSION=${OBS_VERSION}" "OBS_DEB_SHA256=${OBS_DEB_SHA256}" "APT_SNAPSHOT=${APT_SNAPSHOT}" \
        > /usr/local/share/malrunar/obs-runtime/build-info.env

# Rebuild only obs-ffmpeg to fix its raw-capture shutdown race. The original
# source archive is already pinned and mirrored by fetch-source.sh.
FROM runtime-base AS ffmpeg-build
ARG APT_SNAPSHOT
ARG OBS_VERSION
ARG OBS_UPSTREAM_VERSION
ARG OBS_PPA_URL
ARG OBS_ORIG_TARBALL_SHA256
COPY patches/ /patches/
COPY ffmpeg-build/ /recipe/
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        build-essential cmake ninja-build pkg-config patch libsimde-dev \
        libavcodec-dev libavfilter-dev libavdevice-dev libavutil-dev libswscale-dev \
        libavformat-dev libswresample-dev libva-dev libpci-dev libdrm-dev libsrt-gnutls-dev librist-dev; \
    malrunar_snapshot_disable; \
    malrunar_retry curl -fsSL --connect-timeout 30 \
        "${OBS_PPA_URL}/+sourcefiles/obs-studio/${OBS_VERSION}/obs-studio_${OBS_UPSTREAM_VERSION}.orig.tar.gz" \
        -o /tmp/obs-source.tar.gz; \
    echo "${OBS_ORIG_TARBALL_SHA256}  /tmp/obs-source.tar.gz" | sha256sum -c -; \
    mkdir /src; tar -xzf /tmp/obs-source.tar.gz --strip-components=1 -C /src; \
    patch -d /src -p1 < /patches/ffmpeg-capture-teardown.patch; \
    cmake -S /recipe -B /build -G Ninja -DOBS_SOURCE=/src -DCMAKE_BUILD_TYPE=Release; \
    cmake --build /build --parallel 2

FROM runtime-base
COPY --from=ffmpeg-build /build/obs-ffmpeg.so /usr/lib/x86_64-linux-gnu/obs-plugins/obs-ffmpeg.so
COPY patches/ /usr/local/share/malrunar/obs-runtime/patches/
COPY ffmpeg-build/ /usr/local/share/malrunar/obs-runtime/ffmpeg-build/
RUN printf '%s\n' 'OBS_FFMPEG_PATCH=ffmpeg-capture-teardown' >> /usr/local/share/malrunar/obs-runtime/build-info.env; \
    ldd /usr/lib/x86_64-linux-gnu/obs-plugins/obs-ffmpeg.so > /tmp/ffmpeg-linked; \
    ! grep -q 'not found' /tmp/ffmpeg-linked; rm /tmp/ffmpeg-linked
