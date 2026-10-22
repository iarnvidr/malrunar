# Example graph: a base image and three FROM-scratch components. Each component holds only its own
# prefix; `tools/malrunar_graph.py graph.toml` builds them and assembles base + components.
# Pinned: base image by digest (the same pin the repository's images use).
ARG EXAMPLE_UBUNTU=ubuntu:26.04@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944
# Received from the graph (the reference of the base node); a FROM can only use a global ARG. BuildKit
# parses every stage, so the ARG needs a placeholder default (`scratch`) for the targets that never use it;
# the tools always pass the real reference to the node that does.
ARG RUNTIME_BASE_IMAGE=scratch

FROM ${EXAMPLE_UBUNTU} AS base
RUN echo "example base" > /etc/example-base

FROM scratch AS c-alpha
COPY components/alpha/ /opt/example/alpha/

FROM scratch AS c-beta
COPY components/beta/ /opt/example/beta/

# gamma is compiled against the base: it builds FROM it, so its hash includes the base's hash.
FROM ${RUNTIME_BASE_IMAGE} AS build-gamma
COPY src/gamma.sh /tmp/gamma.sh
RUN mkdir -p /out && sh /tmp/gamma.sh > /out/gamma.txt

FROM scratch AS c-gamma
COPY --from=build-gamma /out/ /opt/example/gamma/
