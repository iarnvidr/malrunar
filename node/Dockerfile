# node: the official Node.js LTS runtime as a component image.
#
# The image is FROM scratch and holds only /opt/malrunar/node (the unmodified nodejs.org linux-x64
# release: bin/node, bin/npm, bin/npx, bin/corepack, lib/node_modules/{npm,corepack}, LICENSE),
# plus README.md and SOURCE.txt. It depends on nothing: node is a self-contained binary that needs
# only the C library of whatever image it is copied into. Nothing is added to PATH and no symlink
# is created outside /opt/malrunar/node; consumers add the directory to PATH themselves:
#   COPY --link --from=ghcr.io/iarnvidr/node:<tag> / /
#   ENV PATH=/opt/malrunar/node/bin:$PATH
# Inputs: a pinned Ubuntu image (FETCH_IMAGE, only for tar, no apt runs here) and the release tarball, whose
# sha256 is checked against NODE_SHA256 (see pins.env).
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG NODE_VERSION
ARG NODE_SHA256
ADD https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz /tmp/node.tar.gz
RUN echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; \
    mkdir -p /out/opt/malrunar/node; \
    tar -xzf /tmp/node.tar.gz --strip-components=1 -C /out/opt/malrunar/node; \
    cd /out/opt/malrunar/node; \
    rm -rf include share CHANGELOG.md README.md; \
    test -x bin/node; \
    printf 'node %s (LTS)\nsource: https://nodejs.org/dist/v%s/node-v%s-linux-x64.tar.gz\nsha256: %s\nunmodified except that include/, share/ and the upstream README and changelog are removed\n' \
        "${NODE_VERSION}" "${NODE_VERSION}" "${NODE_VERSION}" "${NODE_SHA256}" > SOURCE.txt

FROM scratch AS component
COPY --link --from=fetch /out/ /
COPY --link README.md /opt/malrunar/node/README.md
