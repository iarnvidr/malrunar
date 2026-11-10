# Public, site-neutral agent development foundation.
# Start from the same digest-pinned code-server base as the private dev runtime.
ARG CODE_SERVER_IMAGE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${CODE_SERVER_IMAGE} AS runtime
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
USER root
COPY --from=node /opt/malrunar/node/ /opt/malrunar/node/
ENV PATH=/opt/malrunar/dagger:/opt/malrunar/node/bin:$PATH
ARG DAGGER_VERSION
ARG DAGGER_CLI_SHA256
ADD https://dl.dagger.io/dagger/releases/${DAGGER_VERSION}/dagger_v${DAGGER_VERSION}_linux_amd64.tar.gz /tmp/dagger.tgz
RUN echo "${DAGGER_CLI_SHA256}  /tmp/dagger.tgz" | sha256sum -c - \
 && mkdir -p /opt/malrunar/dagger \
 && tar -xzf /tmp/dagger.tgz -C /opt/malrunar/dagger dagger \
 && chmod 0755 /opt/malrunar/dagger/dagger \
 && rm /tmp/dagger.tgz \
 && /opt/malrunar/node/bin/node --version \
 && /opt/malrunar/dagger/dagger --help >/dev/null
RUN git config --system maintenance.autoDetach false \
 && git config --system gc.autoDetach false
USER coder
WORKDIR /home/coder
