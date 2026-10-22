# Conventions

Every image `<image>` (desktop-base, obs-runtime, obs-control, guacd, tail-browser-mcp, media-bridge) has:

- `<image>/Dockerfile`: build context is `<image>/` only (copy nothing from outside it).
- `<image>/pins.env`: `KEY=VALUE` lines, passed as `--build-arg`. Must define `VERSION` (the human tag).
  It must not redefine a key of the root `pins.env`.
- `<image>/test/smoke.sh <image-ref>`: runs against a locally loaded image. Not part of the content hash.
  Every container it runs is bounded (`timeout -k 10 300 docker run ...`); never `obs --version`
  (it starts the GUI): take the OBS version from the startup log or from obs-websocket.
- Unit tests of an image's own code live in `<image>/test/` too and run inside the built image from
  its smoke test (stdlib `unittest`; the image's pinned packages are the test dependencies). Tests that
  need more than one container are `<image>/test/e2e/` and have their own job in `pr.yml`.

Root `pins.env` provides `UBUNTU_IMAGE` and `APT_SNAPSHOT`. Dockerfiles declare
`ARG UBUNTU_IMAGE`, `ARG APT_SNAPSHOT` before the first FROM as needed. No default values in
Dockerfiles for pinned inputs: the pins files are the single source.

An image built FROM another one of ours declares an `ARG` named after its parent in upper case with
`_` for `-` and uses it in `FROM`: `obs-runtime` declares `ARG DESKTOP_BASE` and uses
`FROM ${DESKTOP_BASE}`, `obs-control` declares `ARG OBS_RUNTIME` and uses `FROM ${OBS_RUNTIME}`.
`scripts/build.sh` passes it (the local tag of the parent's build, or `PARENT_REF`) and the publish
workflow builds in parent-first order.

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

Services (images with their own long-running program, such as `obs-control`) run as a non-root user
created in the image, read all settings from `MALRUNAR_<NAME>_*` variables or a documented config file
(`MALRUNAR_OBS_*` for obs-control), validate them at start with messages that name the setting, and
document every setting, route and extension point under `docs/`. A test keeps those docs equal to the code.

`scripts/check-public-hygiene.sh` (run by `pr.yml`) rejects private names, internal addresses and
private environment names in any tracked file.
