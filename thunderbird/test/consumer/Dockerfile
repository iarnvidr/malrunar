# Test consumers for the thunderbird component, built by test/smoke.sh. The component is copied
# into a base image the way a real consumer does it, plus a desktop user ("tester", uid 1000):
#   BASE=desktop-base image   the supported composition
#   BASE=pinned Ubuntu image  desktop-base absent: used to check that the dependency is reported clearly
ARG THUNDERBIRD_IMAGE
ARG BASE
FROM ${THUNDERBIRD_IMAGE} AS component

FROM ${BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=component / /
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi; \
    if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi; \
    useradd --create-home --uid 1000 --shell /bin/bash tester
