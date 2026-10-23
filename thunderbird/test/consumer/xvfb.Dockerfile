# The desktop test consumer plus a virtual X server (test only: desktop-base has no X server without
# an xrdp session). Uses the apt snapshot helper that desktop-base installs.
ARG CONSUMER
FROM ${CONSUMER}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends xvfb; \
    malrunar_snapshot_disable
