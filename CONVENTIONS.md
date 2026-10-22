# Conventions

Every image `<image>` (desktop-base, obs-runtime, guacd, tail-browser-mcp) has:

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

Apt snapshot snippet (the ubuntu base image has no ca-certificates, so the snapshot is read over
http; every index is verified through its GPG signature with the Ubuntu archive keyring; never
disable verification):

    RUN . /etc/os-release && \
        rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* && \
        printf 'Types: deb\nURIs: http://snapshot.ubuntu.com/ubuntu/%s/\nSuites: %s %s-updates %s-security\nComponents: main universe restricted multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
            "$APT_SNAPSHOT" "$VERSION_CODENAME" "$VERSION_CODENAME" "$VERSION_CODENAME" \
            > /etc/apt/sources.list.d/ubuntu.sources

(The snapshot source must be written in every stage that runs apt. Add `-o Acquire::Check-Valid-Until=false`
only if a snapshot Release file is older than its Valid-Until, which snapshot.ubuntu.com does not set.)
