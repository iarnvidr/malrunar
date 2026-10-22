# Test consumer for the node component: the bare pinned Ubuntu image (no desktop-base, no apt packages)
# plus the component. It proves node needs nothing else.
ARG NODE_IMAGE
ARG UBUNTU_IMAGE
FROM ${NODE_IMAGE} AS node

FROM ${UBUNTU_IMAGE}
COPY --link --from=node / /
ENV PATH=/opt/malrunar/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
