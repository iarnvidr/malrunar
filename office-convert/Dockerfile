# office-convert: LibreOffice document -> PDF over HTTP.
#
# A small service (/opt/malrunar/office-convert/convert.py, Python standard library only) that turns
# .pptx .ppt .odp .key .docx .doc .odt into a PDF with LibreOffice headless. One conversion at a time,
# no state kept between requests. LibreOffice runs only for the duration of a request and is killed
# (with its child processes) when it exceeds the timeout.
#
# Only the Impress and Writer import filters are installed (the -nogui packages: no GUI toolkit, no
# Java). Metric-compatible fonts (Liberation for Arial/Times/Courier, Carlito for Calibri) keep the
# layout of Office documents.
#
# Settings are MALRUNAR_OFFICE_* variables, documented in office-convert/README.md. The service has NO
# authentication and, by default, listens on 127.0.0.1 only: in a container set
# MALRUNAR_OFFICE_BIND=0.0.0.0 and keep the published port on a trusted network.
# The image runs as uid/gid 65534 (nobody) and needs no writable volume (it uses /tmp).
ARG UBUNTU_IMAGE
FROM ${UBUNTU_IMAGE}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG APT_SNAPSHOT
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends \
        libreoffice-impress-nogui libreoffice-writer-nogui python3 fontconfig \
        fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core; \
    malrunar_snapshot_disable; \
    soffice --version
COPY convert.py /opt/malrunar/office-convert/convert.py
RUN python3 -I -m py_compile /opt/malrunar/office-convert/convert.py; \
    rm -rf /opt/malrunar/office-convert/__pycache__
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /tmp
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-I", "/opt/malrunar/office-convert/convert.py", "--healthcheck"]
ENTRYPOINT ["python3", "-I", "/opt/malrunar/office-convert/convert.py"]
