# Test consumers for the msteams-mcp component, built by test/smoke.sh. The components are copied into
# a base image the way a real consumer does it, plus a desktop user ("tester", uid 1000). A component
# that a test wants ABSENT is passed as `scratch` (an empty stage), so one recipe builds every variant:
#   BASE=desktop-base image, NODE_IMAGE=node, WORK_BROWSER_IMAGE=work-browser   the supported composition
#   BASE=pinned Ubuntu image, or any of the other two = scratch                 a dependency missing: used to check that it is reported clearly
ARG BASE
ARG NODE_IMAGE=scratch
ARG WORK_BROWSER_IMAGE=scratch
ARG MSTEAMS_MCP_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${WORK_BROWSER_IMAGE} AS work-browser
FROM ${MSTEAMS_MCP_IMAGE} AS component

FROM ${BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=node / /
COPY --link --from=work-browser / /
COPY --link --from=component / /
ENV PATH=/opt/malrunar/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi; \
    if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi; \
    useradd --create-home --uid 1000 --shell /bin/bash tester
