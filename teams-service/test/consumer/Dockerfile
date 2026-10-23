# Test consumers for the teams-service component, built by test/smoke.sh. The components are copied into
# a base image the way a real consumer does it, plus a user ("tester", uid 1000). A component that a test
# wants ABSENT is passed as `scratch` (an empty stage), so one recipe builds every variant:
#   BASE=pinned Ubuntu image, NODE_IMAGE=node, MSTEAMS_MCP_IMAGE=msteams-mcp   the declared dependencies and nothing else
#   BASE=desktop-base image                                                     the same, plus supervisord, for the fragment test
#   NODE_IMAGE or MSTEAMS_MCP_IMAGE = scratch                                   a dependency missing: used to check that it is reported clearly
ARG BASE
ARG NODE_IMAGE=scratch
ARG MSTEAMS_MCP_IMAGE=scratch
ARG TEAMS_SERVICE_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${MSTEAMS_MCP_IMAGE} AS msteams-mcp
FROM ${TEAMS_SERVICE_IMAGE} AS component

FROM ${BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=node / /
COPY --link --from=msteams-mcp / /
COPY --link --from=component / /
ENV PATH=/opt/malrunar/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi; \
    if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi; \
    useradd --create-home --uid 1000 --shell /bin/bash tester
