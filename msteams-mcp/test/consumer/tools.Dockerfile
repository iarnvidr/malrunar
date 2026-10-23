# The full test consumer plus what a real consumer adds for the optional visual browser MCP
# (xdotool, imagemagick, xclip, poppler-utils: its declared extra dependencies) and, for the test only,
# a virtual X server. Uses the apt snapshot helper that desktop-base installs.
ARG CONSUMER
FROM ${CONSUMER}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends xdotool imagemagick xclip poppler-utils xvfb; \
    malrunar_snapshot_disable
