# Quickstart desktop: desktop-base + the media-bridge component, with PipeWire for a user that
# has no sound card. The image refs are build args so that compose (or CI) can substitute images
# built from a checkout. Pin by digest in production.
ARG DESKTOP_BASE=ghcr.io/iarnvidr/desktop-base:153.5.1esr
ARG MEDIA_BRIDGE=ghcr.io/iarnvidr/media-bridge:0.1.0

FROM ${MEDIA_BRIDGE} AS bridge

FROM ${DESKTOP_BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=bridge / /
# The desktop user, its runtime directory (PipeWire and pipewire-pulse put their sockets there) and
# a WirePlumber configuration without sound cards, cameras or logind (desktop-base ships it as an
# example: the virtual microphone is all this container needs).
RUN useradd --create-home --uid 1000 --shell /bin/bash desktop; \
    install -d -o desktop -g desktop -m 0700 /run/user/1000; \
    install -D -o desktop -g desktop -m 0644 \
        /usr/local/share/malrunar/desktop-base/examples/wireplumber-virtual-only.conf.example \
        /home/desktop/.config/wireplumber/wireplumber.conf.d/virtual-only.conf; \
    chown -R desktop:desktop /home/desktop/.config
COPY --chmod=0755 audio-session.sh bridge-start.sh /usr/local/bin/
COPY supervisord.conf /etc/supervisor/quickstart.conf

# Settings the media-bridge reads (see media-bridge/README.md). compose.yaml sets the same ones.
ENV XDG_RUNTIME_DIR=/run/user/1000 \
    MALRUNAR_BRIDGE_START_OUTPUT_URL=http://obs:18095/api/start-output \
    MALRUNAR_BRIDGE_VIDEO_SINK=null

# supervisord runs as root and starts the programs as the user "desktop" (supervisord.conf).
HEALTHCHECK --interval=5s --timeout=10s --start-period=60s --retries=12 \
    CMD runuser -u desktop -- pactl list short sources | grep -q "${MALRUNAR_BRIDGE_MIC_NAME:-malrunar_virtual_mic}"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "--nodaemon", "--configuration", "/etc/supervisor/quickstart.conf"]
