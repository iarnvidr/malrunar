# The desktop test consumer plus what a real consumer adds for the window tools: xdotool (the kit's
# declared extra dependency), and, for the test only, a virtual X server. Uses the apt snapshot helper
# that desktop-base installs.
ARG CONSUMER
FROM ${CONSUMER}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    if (malrunar_snapshot_enable && malrunar_retry malrunar_apt install -y --no-install-recommends xdotool xvfb); then \
      malrunar_snapshot_disable; \
    else \
      echo "WARNING: Ubuntu snapshot unavailable; using signed live Ubuntu archive for throwaway integration-test packages" >&2; \
      malrunar_snapshot_disable; \
      malrunar_retry malrunar_apt update; \
      malrunar_retry malrunar_apt install -y --no-install-recommends xdotool xvfb; \
      rm -rf /var/lib/apt/lists/*; \
    fi
