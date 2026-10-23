# guacamole-tunnel: several viewers share one Guacamole connection instead of evicting each other.
#
# A small service (/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py, Python with the distro's
# aiohttp) that runs between your reverse proxy and the Guacamole web application and turns a second
# "open this connection" request of the same user into a join of the session that is already running.
# It is a service of its own and changes nothing in guacd: use it only if you want the behaviour.
#
# Settings are MALRUNAR_GUAC_TUNNEL_* variables, documented in guacamole-tunnel/README.md. The
# upstream Guacamole URL is required; an invalid setting makes the service exit with status 78 and a
# message naming it. Who the user is comes from a header set by your authenticating proxy, trusted only
# per MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES / _PROXY_SECRET_* (forward_auth.py); with neither, nobody is trusted. The image runs as uid/gid 65534
# (nobody), keeps no state and listens on 0.0.0.0:8082 inside the container.
ARG UBUNTU_IMAGE
FROM ${UBUNTU_IMAGE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends python3 python3-aiohttp python3-yarl; \
    malrunar_snapshot_disable
COPY guacamole_tunnel.py forward_auth.py /opt/malrunar/guacamole-tunnel/
RUN python3 -I -m py_compile /opt/malrunar/guacamole-tunnel/guacamole_tunnel.py /opt/malrunar/guacamole-tunnel/forward_auth.py; \
    rm -rf /opt/malrunar/guacamole-tunnel/__pycache__
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALRUNAR_GUAC_TUNNEL_BIND=0.0.0.0
USER 65534:65534
WORKDIR /tmp
EXPOSE 8082
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-I", "/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py", "--healthcheck"]
ENTRYPOINT ["python3", "-I", "/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py"]
