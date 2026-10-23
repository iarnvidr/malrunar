# obs-control: headless OBS driven over HTTP, on top of obs-runtime.
#
# obs-runtime (OBS Studio with its browser source on desktop-base) plus the obs-control service
# (/opt/malrunar/obs-control): a controller that generates the OBS profile and scene collection
# from a declarative config, starts OBS on a virtual display, and serves an HTTP API for scene
# switching, program previews, clips, recording, input levels, output control and automatic b-roll,
# plus a UDP/MPEG-TS relay (`entrypoint.sh relay`). Plugins add routes and scenes
# (docs/obs-control-api.md).
#
# Python 3 comes from desktop-base; aiohttp and websockets (and poppler-utils, whose pdftoppm renders
# uploaded PDF slides for the optional takes plugin) come from the Ubuntu archive at the pinned
# snapshot (apt via scripts/apt-snapshot.sh), so nothing is installed from PyPI.
#
# The image runs as the user `obs` (uid/gid 1000, home /config). Mount your config at
# /etc/malrunar/obs-control.toml (or set MALRUNAR_OBS_CONFIG) and a volume at /config for the OBS
# profile, recordings and state. Everything is configured with MALRUNAR_OBS_* variables or the
# config file; there are no defaults that point at a site.
ARG OBS_RUNTIME
FROM ${OBS_RUNTIME}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends python3-aiohttp python3-websockets poppler-utils; \
    malrunar_snapshot_disable
RUN groupadd --gid 1000 obs; \
    useradd --uid 1000 --gid 1000 --home-dir /config --no-create-home --shell /usr/sbin/nologin obs; \
    install -d -o 1000 -g 1000 -m 0755 /config
COPY malrunar_obs /opt/malrunar/obs-control/lib/malrunar_obs
COPY entrypoint.sh /opt/malrunar/obs-control/entrypoint.sh
COPY examples/ /usr/local/share/malrunar/obs-control/examples/
RUN chmod 0755 /opt/malrunar/obs-control/entrypoint.sh; \
    PYTHONPATH=/opt/malrunar/obs-control/lib python3 -c 'import aiohttp, websockets, malrunar_obs.controller, malrunar_obs.relay, malrunar_obs.takes'; \
    command -v pdftoppm pdfinfo >/dev/null
USER 1000:1000
EXPOSE 18095
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=8 \
    CMD curl -fsS "http://127.0.0.1:${MALRUNAR_OBS_CONTROL_PORT:-18095}/ready" | grep -q '"ready": true'
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/malrunar/obs-control/entrypoint.sh"]
