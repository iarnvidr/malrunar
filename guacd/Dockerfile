# guacd (Apache Guacamole proxy daemon) 1.6.0 with the RDP GDI fix, on pinned Ubuntu 26.04.
#
# A standalone, runnable image: guacd plus the exact distro libraries it links against,
# with their copyright files. Build inputs are all pinned: base image by digest, apt by a
# snapshot.ubuntu.com date, sources by sha256 (see pins.env). Build with scripts/build.sh guacd.
#
# Runs as uid 1000 (guacd), with writable HOME=/home/guacd.
# and listens on 0.0.0.0:4822. guacd has no authentication, so never publish the port beyond
# the network that talks to it; bind it to loopback with `guacd -f -b 127.0.0.1 -l 4822`.
ARG UBUNTU_IMAGE

# --- build ------------------------------------------------------------------------------------
FROM ${UBUNTU_IMAGE} AS build
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG APT_SNAPSHOT
ARG GUACAMOLE_VERSION
ARG GUACAMOLE_SERVER_SHA256
ARG GUACAMOLE_WOL_COMMIT
ARG GUACAMOLE_WOL_SHA256
ARG GUACAMOLE_SOCKET_NEST_COMMIT
ARG GUACAMOLE_SOCKET_NEST_SHA256
# apt reads the pinned snapshot over TLS (snapshot.ubuntu.com redirects http to https); the helper
# bootstraps ca-certificates first. Index signatures and certificates are always verified.
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt upgrade -y; \
    malrunar_retry malrunar_apt install -y --no-install-recommends ca-certificates curl \
        gcc g++ make pkg-config libcairo2-dev libjpeg-dev libpng-dev uuid-dev \
        freerdp3-dev libpulse-dev libssl-dev libwebp-dev perl libcunit1-dev patch; \
    malrunar_snapshot_disable
# Later steps keep their original /bin/sh semantics (no pipefail in the ldd/dpkg pipeline).
SHELL ["/bin/sh", "-c"]
COPY guacamole-rdp-gdi.patch /tmp/rdp-gdi.patch
# The rpath makes the daemon, libguac and the protocol plugins (dlopen'ed by libguac) find
# /usr/local/lib without an ld.so cache entry.
RUN set -eu; \
    curl -fsSL --retry 5 --retry-all-errors "https://archive.apache.org/dist/guacamole/${GUACAMOLE_VERSION}/source/guacamole-server-${GUACAMOLE_VERSION}.tar.gz" -o /tmp/guacamole.tar.gz; \
    echo "${GUACAMOLE_SERVER_SHA256}  /tmp/guacamole.tar.gz" | sha256sum -c -; \
    mkdir /tmp/guacamole; tar -xzf /tmp/guacamole.tar.gz --strip-components=1 -C /tmp/guacamole; \
    cd /tmp/guacamole; \
    CFLAGS='-O2 -std=gnu17 -isystem /usr/include/freerdp3 -isystem /usr/include/winpr3 -Wno-error=deprecated-declarations' \
    LDFLAGS='-Wl,-rpath,/usr/local/lib' \
        ./configure --prefix=/usr/local --disable-guacenc --disable-guaclog; \
    grep -q '^#define _XOPEN_SOURCE 700$' config.h; \
    sed -i 's/^#define _XOPEN_SOURCE 700$/#ifndef _XOPEN_SOURCE\n#define _XOPEN_SOURCE 700\n#endif/' config.h; \
    grep -qF '(chr = memchr(haystack + off, needle[0], len - off))' src/libguac/string.c; \
    sed -i 's/chr = memchr(/chr = (char*) memchr(/' src/libguac/string.c; \
    curl -fsSL --retry 5 --retry-all-errors "https://raw.githubusercontent.com/apache/guacamole-server/${GUACAMOLE_WOL_COMMIT}/src/libguac/wol.c" -o src/libguac/wol.c; \
    echo "${GUACAMOLE_WOL_SHA256}  src/libguac/wol.c" | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-all-errors "https://raw.githubusercontent.com/apache/guacamole-server/${GUACAMOLE_SOCKET_NEST_COMMIT}/src/libguac/socket-nest.c" -o src/libguac/socket-nest.c; \
    echo "${GUACAMOLE_SOCKET_NEST_SHA256}  src/libguac/socket-nest.c" | sha256sum -c -; \
    patch -p1 --fuzz=0 < /tmp/rdp-gdi.patch; \
    make -j"$(nproc)"; \
    (timeout 120s make check || { cat src/libguac/tests/test-suite.log; exit 1; }); \
    make DESTDIR=/out install; \
    test -r /out/usr/local/lib/libguac-client-rdp.so; \
    mkdir -p /out/usr/local/share/malrunar/licenses/guacamole-server; \
    cp LICENSE /out/usr/local/share/malrunar/licenses/guacamole-server/; \
    if [ -f NOTICE ]; then cp NOTICE /out/usr/local/share/malrunar/licenses/guacamole-server/; fi; \
    cp /tmp/rdp-gdi.patch /out/usr/local/share/malrunar/licenses/guacamole-server/guacamole-rdp-gdi.patch; \
    printf 'guacamole-server %s\nsource: https://archive.apache.org/dist/guacamole/%s/source/guacamole-server-%s.tar.gz (sha256 %s)\nreplaced files: src/libguac/wol.c from apache/guacamole-server commit %s (sha256 %s), src/libguac/socket-nest.c from commit %s (sha256 %s)\npatch: guacamole-rdp-gdi.patch (backport of GUACAMOLE-2328, upstream commit ab36756b596520ae2a94cd4d25e406e0b5aa820b)\n' \
        "$GUACAMOLE_VERSION" "$GUACAMOLE_VERSION" "$GUACAMOLE_VERSION" "$GUACAMOLE_SERVER_SHA256" \
        "$GUACAMOLE_WOL_COMMIT" "$GUACAMOLE_WOL_SHA256" "$GUACAMOLE_SOCKET_NEST_COMMIT" "$GUACAMOLE_SOCKET_NEST_SHA256" \
        > /out/usr/local/share/malrunar/licenses/guacamole-server/SOURCE.txt
# Record which distro packages guacd and its plugins link against, at which versions. The smoke
# test compares this with what the final image ships.
RUN set -eu; export LD_LIBRARY_PATH=/out/usr/local/lib; \
    find /out -type f \( -name guacd -o -name '*.so*' \) -exec ldd {} + 2>/dev/null \
        | awk '$3 ~ /^\// {print $3}' | grep -v '^/out/' | sort -u \
        | while read -r lib; do dpkg -S "$(readlink -f "$lib")" | head -1 | cut -d: -f1; done | sort -u \
        | xargs dpkg-query -W -f='${binary:Package} ${Version}\n' > /out/usr/local/share/malrunar/guacd-linked-packages; \
    test -s /out/usr/local/share/malrunar/guacd-linked-packages

# --- runtime image ----------------------------------------------------------------------------
FROM ${UBUNTU_IMAGE} AS runtime
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG APT_SNAPSHOT
# Keep the distro copyright files of every installed library (dpkg path excludes, where the base
# image has them, would drop them).
RUN --mount=type=bind,source=scripts,target=/scripts \
    rm -f /etc/dpkg/dpkg.cfg.d/excludes /etc/dpkg/dpkg.cfg.d/*docker*; \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt upgrade -y; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        libcairo2 libjpeg-turbo8 libpng16-16t64 libssl3t64 libwebp7 libpulse0 \
        libfreerdp3-3 libfreerdp-client3-3 libwinpr3-3; \
    malrunar_snapshot_disable
SHELL ["/bin/sh", "-c"]
COPY --from=build /out/ /
# Fail the build if a library is missing, or a linked package version differs from what is installed,
# or a copyright file is absent.
RUN set -eu; \
    /usr/local/sbin/guacd -v; \
    for binary in /usr/local/sbin/guacd /usr/local/lib/libguac-client-rdp.so; do \
        if ldd "$binary" | grep -q 'not found'; then ldd "$binary" >&2; exit 1; fi; \
    done; \
    while read -r package version; do \
        test "$(dpkg-query -W -f='${binary:Package} ${Version}' "$package")" = "$package $version"; \
        test -s "/usr/share/doc/${package%%:*}/copyright"; \
    done < /usr/local/share/malrunar/guacd-linked-packages
RUN set -eu; \
    useradd --create-home --uid 1000 --user-group --shell /usr/sbin/nologin guacd; \
    chown guacd:guacd /home/guacd
USER guacd:guacd
ENV HOME=/home/guacd
LABEL org.opencontainers.image.title="guacd" \
      org.opencontainers.image.description="Apache Guacamole guacd 1.6.0 with the RDP GDI fix on Ubuntu 26.04" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/iarnvidr/malrunar"
EXPOSE 4822
CMD ["/usr/local/sbin/guacd", "-f", "-b", "0.0.0.0", "-l", "4822"]
