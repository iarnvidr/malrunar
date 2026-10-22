# Test consumer: a desktop built on desktop-base with the media-bridge component copied in, the way
# a real consumer does it. Built by test/smoke.sh with the locally built component as BRIDGE_IMAGE.
ARG BRIDGE_IMAGE
ARG DESKTOP_BASE
FROM ${BRIDGE_IMAGE} AS bridge

FROM ${DESKTOP_BASE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY --link --from=bridge / /
RUN useradd --create-home --uid 1000 --shell /bin/bash tester; \
    install -d -o tester -g tester -m 700 /run/user/1000
COPY --chmod=755 bridge-test.sh /usr/local/bin/bridge-test.sh
