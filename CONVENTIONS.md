# Conventions

Every image `<image>` (desktop-base, obs-runtime, guacd, tail-browser-mcp, media-bridge) has:

- `<image>/Dockerfile`: build context is `<image>/` only (copy nothing from outside it).
- `<image>/pins.env`: `KEY=VALUE` lines, passed as `--build-arg`. Must define `VERSION` (the human tag).
  It must not redefine a key of the root `pins.env`.
- `<image>/test/smoke.sh <image-ref>`: runs against a locally loaded image. Not part of the content hash.

Root `pins.env` provides `UBUNTU_IMAGE` and `APT_SNAPSHOT`. Dockerfiles declare
`ARG UBUNTU_IMAGE`, `ARG APT_SNAPSHOT` before the first FROM as needed. No default values in
Dockerfiles for pinned inputs: the pins files are the single source.

`obs-runtime` declares `ARG DESKTOP_BASE` and uses `FROM ${DESKTOP_BASE}`.

Install prefixes: `/opt/malrunar/<component>`, `/usr/local/share/malrunar/...`. No compat symlinks.

Hash: `scripts/malrunar_image.py hash <image>` = f(files of the image directory except `test/`,
root pins, image pins, parent hash). Tags: `<version>` and `h-<hash>`.

Apt: every stage that runs apt uses the snapshot helper (`<image>/scripts/apt-snapshot.sh`, one copy
per image because the build context is the image directory). The Ubuntu base image has no CA bundle
and snapshot.ubuntu.com redirects http to https, so the helper installs ca-certificates from the
image's own archive sources first, then switches to https://snapshot.ubuntu.com/<APT_SNAPSHOT>.
Signature and certificate verification are never disabled.

    SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
    RUN --mount=type=bind,source=scripts,target=/scripts \
        . /scripts/apt-snapshot.sh; \
        malrunar_snapshot_enable; \
        malrunar_retry malrunar_apt install -y --no-install-recommends <packages>; \
        malrunar_snapshot_disable

Component images (`tail-browser-mcp`, `media-bridge`) are `FROM scratch` and hold only
`/opt/malrunar/<component>`; consumers add them with `COPY --link --from=<image> / /`. A component
that needs a runtime from `desktop-base` (ffmpeg, PipeWire tools) documents that and is smoke-tested
in a consumer image built on `desktop-base` (`<image>/test/consumer/Dockerfile`), not on its own.
Tests that need host features (kernel modules, devices) take them as optional inputs and fall back
to a documented mode, and the CI result is recorded.

Environment variables a component reads are named `MALRUNAR_<COMPONENT>_*` and are all optional,
with defaults documented in the component's README. Nothing site-specific (addresses, host names,
product names) belongs in a recipe, a default, a comment or a test.
