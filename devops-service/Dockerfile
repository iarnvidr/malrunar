# Self-contained opt-in service foundation; no consumer policy, login or topic index.
# Do not COPY its venv or native libraries into a different distribution/Python ABI.
ARG UBUNTU_IMAGE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node

FROM ${UBUNTU_IMAGE} AS runtime
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends ca-certificates python3 python3-venv libffi8 libssl3t64; \
    malrunar_snapshot_disable
COPY --from=node /opt/malrunar/node/ /opt/malrunar/node/
ENV PATH=/opt/malrunar/devops-service/venv/bin:/opt/malrunar/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

FROM runtime AS dependencies
ARG TARGETARCH
WORKDIR /opt/malrunar/devops-service
COPY requirements.txt requirements.lock package.json package-lock.json ./
RUN test "$TARGETARCH" = amd64 \
 && python3 -c 'import sys; assert sys.version_info[:2] == (3, 14), "lock requires Python 3.14"' \
 && python3 -m venv venv \
 && venv/bin/pip --isolated install --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements.lock \
 && venv/bin/pip check \
 && node /opt/malrunar/node/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts --omit=optional --no-audit --no-fund

FROM runtime AS service
COPY --from=dependencies /opt/malrunar/devops-service/ /opt/malrunar/devops-service/
COPY src/ /opt/malrunar/devops-service/src/
COPY README.md LICENSE SOURCE.txt /opt/malrunar/devops-service/
RUN groupadd --gid 10001 devops \
 && useradd --uid 10001 --gid 10001 --home-dir /state --no-create-home devops \
 && install -d -m 0700 -o 10001 -g 10001 /state
ENV PYTHONPATH=/opt/malrunar/devops-service/src HOME=/state PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/malrunar/devops-service
USER 10001:10001
ENTRYPOINT ["/opt/malrunar/devops-service/venv/bin/python", "-m", "malrunar_devops.run"]
