# Test consumers for the thunderbird-mcp component, built by test/smoke.sh with --target:
#   bare       the pinned Ubuntu image + the component: node is absent (checks that the dependency is reported)
#   with-node  the same + the node component: what the component declares it needs, and nothing else
ARG MCP_IMAGE
ARG NODE_IMAGE
ARG BASE
FROM ${MCP_IMAGE} AS mcp
FROM ${NODE_IMAGE} AS node

FROM ${BASE} AS bare
COPY --link --from=mcp / /

FROM bare AS with-node
COPY --link --from=node / /
