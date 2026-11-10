# An end-to-end image for the test only: desktop-base + the thunderbird, node and thunderbird-mcp components, a
# desktop user, a virtual X server, and the policy fragment the component ships that installs its add-on into
# Thunderbird (a modification of Thunderbird that this throwaway image makes and never publishes).
ARG BASE
ARG THUNDERBIRD_IMAGE
ARG NODE_IMAGE
ARG MCP_IMAGE
FROM ${THUNDERBIRD_IMAGE} AS thunderbird
FROM ${NODE_IMAGE} AS node
FROM ${MCP_IMAGE} AS mcp

FROM ${BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
COPY --link --from=thunderbird / /
COPY --link --from=node / /
COPY --link --from=mcp / /
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi; \
    if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi; \
    useradd --create-home --uid 1000 --shell /bin/bash tester; \
    . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    if (malrunar_snapshot_enable && malrunar_retry malrunar_apt install -y --no-install-recommends xvfb); then \
      malrunar_snapshot_disable; \
    else \
      echo "WARNING: Ubuntu snapshot unavailable; using signed live Ubuntu archive for throwaway integration-test packages" >&2; \
      malrunar_snapshot_disable; \
      malrunar_retry malrunar_apt update; \
      malrunar_retry malrunar_apt install -y --no-install-recommends xvfb; \
      rm -rf /var/lib/apt/lists/*; \
    fi
RUN python3 /opt/malrunar/thunderbird/consumer/customize.py policies /opt/malrunar/thunderbird-mcp/share/policies-extension.json.example
