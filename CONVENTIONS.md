# Conventions

Every image `<image>` (desktop-base, obs-runtime, obs-control, guacd, tail-browser-mcp, media-bridge, office-convert, node, work-browser, session-kit) has:

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
Dockerfiles for pinned inputs: the pins files are the single source. A root pin only counts for an
image whose Dockerfile declares it (see Hash), so declare exactly the pins the build uses; an input
that never reaches the image (a build tool image that only unpacks a download) is better pinned in
the image's own `pins.env` under its own name, so a weekly bump of the shared pin does not republish it.

An image built FROM another one of ours declares an `ARG` named after its parent in upper case with
`_` for `-` and uses it in `FROM`: `obs-runtime` declares `ARG DESKTOP_BASE` and uses
`FROM ${DESKTOP_BASE}`, `obs-control` declares `ARG OBS_RUNTIME` and uses `FROM ${OBS_RUNTIME}`.
`scripts/build.sh` passes it (the local tag of the parent's build, or `PARENT_REF`) and the publish
workflow builds in parent-first order.

Install prefixes: `/opt/malrunar/<component>`, `/usr/local/share/malrunar/...`. No compat symlinks.

Hash: `scripts/malrunar_image.py hash <image>` = f(files of the image directory except `test/`,
the root pins its Dockerfile declares as `ARG`, image pins, parent hash). Tags: `<version>` and
`h-<hash>`. A pin an image does not declare cannot change it, so the weekly pin bump does not
republish it (`media-bridge`, `work-browser` and the other scripts-only components never change with
a bump). `scripts/changed_images.py <base> --republish` lists what a change would publish, and
`pr.yml` builds only the images a change touches (their hash, any file under their directory including
`test/`, and the images they are tested with); a change to `scripts/build.sh`, `scripts/smoke.sh` or
`pr.yml` builds all of them. `scripts/test/` holds the tests of this logic.

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

Component images (`tail-browser-mcp`, `media-bridge`, `node`, `work-browser`, `session-kit`) are `FROM scratch` and
hold only `/opt/malrunar/<component>` (and, for a supervisord program, the one fragment described
below); consumers add them with `COPY --link --from=<image> / /`. A component that needs a runtime
from `desktop-base` (ffmpeg, PipeWire tools, Firefox) documents that and is smoke-tested in a consumer
image built on `desktop-base` (`<image>/test/consumer/Dockerfile`), not on its own.

Everything is opt-in and composable. A product image is `desktop-base` plus an explicit list of
components, each added by its own `COPY --link --from=...` line; there is no component that another
one pulls in. A component therefore:

- declares its dependencies in its README, in a "Dependencies" section (`node`: none; `work-browser`:
  `desktop-base` only), and never silently relies on another component being present;
- enforces them in its smoke test: the test image holds the declared dependencies and nothing else
  (the `node` test runs in the bare Ubuntu image, the `work-browser` test has no `node`), and a
  component that needs `desktop-base` is run once without it and must fail with a message that names
  it (exit status 78, "configuration error");
- reads settings only from `MALRUNAR_<COMPONENT>_*` variables: no user, path, secret or host name is
  built in, and a missing setting that cannot have a default is reported by name.

### supervisord programs of components

A component that runs a long-lived program ships its supervisord program as one file,
`/etc/supervisor/conf.d/malrunar-<component>.conf`, with one `[program:<component>]` section. That is
the include directory of `desktop-base`'s supervisor (`files = /etc/supervisor/conf.d/*.conf`), so the
program exists in an image exactly when the component was copied in: there is no list to edit and
nothing to enable, and an image without the component has no trace of the program. The convention:

- the file name and the program name carry the component name, so fragments of different components
  never collide, and no fragment is shared;
- a fragment has no user, path, host or secret: it runs the component's own script from
  `/opt/malrunar/<component>/`, and the script reads its settings from the `MALRUNAR_<COMPONENT>_*`
  environment that supervisord inherits (`ENV`, `docker run -e`, compose `environment:`); a script
  that must run as a desktop user starts as root and switches to the user named by a variable;
- a program that cannot work in the image (a dependency missing, a setting absent) exits with status 78
  and a message, and its fragment uses `exitcodes=0`, `startsecs` and `startretries`, so supervisord
  stops retrying and shows the cause instead of restarting forever;
- an image that does not want the program deletes the file in its own layer, or the component offers
  an `..._ENABLED=0` switch that makes the script exit successfully;
- the smoke test checks that the fragment is the only thing the component adds outside its prefix.

Components built before this convention (`media-bridge`) ship their supervisord and systemd programs
as `share/*.example` files to copy and edit; they are not active until the consumer installs them.
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
