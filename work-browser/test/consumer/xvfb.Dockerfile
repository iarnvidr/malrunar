# Throwaway display-test consumer only. Production images remain pinned to the Ubuntu snapshot.
# When the snapshot service is temporarily unavailable, install the test-only Xvfb from the
# signed Ubuntu archive, and clearly report the exception. Neither image is published.
ARG CONSUMER
FROM ${CONSUMER}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN test -n "${APT_SNAPSHOT}" && . /usr/local/share/malrunar/lib/apt-snapshot.sh; \
    if (malrunar_snapshot_enable && malrunar_retry malrunar_apt install -y --no-install-recommends xvfb); then \
      malrunar_snapshot_disable; \
    else \
      echo "WARNING: Ubuntu snapshot unavailable; using the signed live Ubuntu archive for throwaway Xvfb smoke test" >&2; \
      malrunar_snapshot_disable; \
      malrunar_retry malrunar_apt update; \
      malrunar_retry malrunar_apt install -y --no-install-recommends xvfb; \
      rm -rf /var/lib/apt/lists/*; \
    fi