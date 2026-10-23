# thunderbird-mcp: the Thunderbird MCP bridge and add-on (https://github.com/TKasperczyk/thunderbird-mcp, MIT)
# as a component image.
#
# The image is FROM scratch and holds only /opt/malrunar/thunderbird-mcp:
#   mcp-bridge.cjs          the stdio MCP server, exactly as upstream publishes it at the pinned commit (needs `node`)
#   package.json            upstream's, for the bridge's reported version
#   thunderbird-mcp.xpi     the add-on that serves the bridge from inside Thunderbird, packed from the pinned source with
#                           the inline-image limits raised (build-xpi.py); install it with a policy or by hand
#   bin/malrunar-thunderbird-mcp   a wrapper: checks that node is there (exit 78 and a message when it is not), maps
#                                  MALRUNAR_THUNDERBIRD_MCP_* settings to the bridge's own, then runs the bridge
#   share/                  a policy fragment that installs the add-on, and an example client configuration
#   UPSTREAM-LICENSE (MIT), LICENSE (Apache-2.0, for the files of this repository), SOURCE.txt, README.md
# It does not contain Thunderbird and does not modify it: the add-on is installed into a Thunderbird by the
# consumer (a policy fragment is provided), which is the consumer's modification to make. It needs the `node`
# component (or any node >= 18) to run, and talks to a Thunderbird that has the add-on installed.
# Nothing is added to PATH. Inputs, all pinned: the upstream commit (asserted), the build image digest, the apt snapshot.
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS build
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG FETCH_APT_SNAPSHOT
RUN --mount=type=bind,source=scripts,target=/scripts \
    export APT_SNAPSHOT="${FETCH_APT_SNAPSHOT}"; \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends ca-certificates git python3; \
    malrunar_snapshot_disable
ARG THUNDERBIRD_MCP_VERSION
ARG THUNDERBIRD_MCP_COMMIT
ARG MAX_INLINE_IMAGE_BASE64_BYTES
ARG MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES
ARG PATCH_SET
RUN git clone --depth 1 --branch "v${THUNDERBIRD_MCP_VERSION}" https://github.com/TKasperczyk/thunderbird-mcp.git /src; \
    test "$(git -C /src rev-parse HEAD)" = "${THUNDERBIRD_MCP_COMMIT}"
COPY build-xpi.py /build-xpi.py
RUN set -eu; \
    out=/out/opt/malrunar/thunderbird-mcp; \
    python3 /build-xpi.py /src "$out" "${MAX_INLINE_IMAGE_BASE64_BYTES}" "${MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES}" "${PATCH_SET}"; \
    install -m 0644 /src/mcp-bridge.cjs /src/package.json "$out/"; \
    install -m 0644 /src/LICENSE "$out/UPSTREAM-LICENSE"; \
    grep -q '^MIT License' "$out/UPSTREAM-LICENSE"; \
    printf 'thunderbird-mcp %s\nsource: https://github.com/TKasperczyk/thunderbird-mcp at commit %s (MIT)\nmcp-bridge.cjs and package.json: as upstream publishes them\nthunderbird-mcp.xpi: packed from the same commit by build-xpi.py in https://github.com/iarnvidr/malrunar (thunderbird-mcp/); only the two inline-image limits (%s and %s base64 bytes) and the add-on version differ from upstream\n' \
        "${THUNDERBIRD_MCP_VERSION}" "${THUNDERBIRD_MCP_COMMIT}" "${MAX_INLINE_IMAGE_BASE64_BYTES}" "${MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES}" > "$out/SOURCE.txt"

FROM scratch AS component
COPY --link --from=build /out/ /
COPY --link LICENSE README.md /opt/malrunar/thunderbird-mcp/
COPY --link bin/ /opt/malrunar/thunderbird-mcp/bin/
COPY --link share/ /opt/malrunar/thunderbird-mcp/share/
