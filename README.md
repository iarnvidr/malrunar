# malrunar

Container images for running a remote desktop with a browser, OBS and a Guacamole gateway, for
example a desktop you join video calls from and stream through OBS. The parts are slow to build and
easy to get subtly wrong (pinned OBS with its browser source, guacd against FreeRDP 3, an xrdp
desktop with working audio), so they are built here once, reproducibly, and published as images you
build on top of.

The images are generic. They create no users and carry no site configuration; you add those in your
own image.

## Images

All images are published to `ghcr.io/iarnvidr/<image>`, for `linux/amd64`.

| Image | Use it for | Contents |
|---|---|---|
| [`desktop-base`](desktop-base/) | The base of a remote desktop image | Ubuntu 26.04, xrdp + xorgxrdp, XFCE, PipeWire with the xrdp audio module, ffmpeg, supervisor, tini, and the official Firefox ESR in `/opt/malrunar/firefox` |
| [`obs-runtime`](obs-runtime/) | A desktop that also runs OBS | `desktop-base` + OBS Studio 32.2.0, including its browser source |
| [`guacd`](guacd/) | The Guacamole proxy daemon, as a container | guacd 1.6.0 built against FreeRDP 3, with an RDP GDI fix, listening on port 4822 |
| [`tail-browser-mcp`](tail-browser-mcp/) | Letting an AI agent drive Firefox | The [Tail](https://github.com/rtf6x/tail-browser-mcp) MCP server, client and Firefox extension in `/opt/malrunar/tail-browser-mcp`. A `FROM scratch` layer you copy into your image |
| [`media-bridge`](media-bridge/) | An OBS-driven virtual camera and microphone for any call app in the desktop | A receiver that turns an MPEG-TS/UDP stream into a v4l2loopback camera and a PipeWire microphone, plus host setup, supervisord/systemd examples and audio/browser helpers in `/opt/malrunar/media-bridge`. A `FROM scratch` layer you copy into your image |

## Using them

Pin by digest in anything you deploy. Tags are listed under [Tags](#tags).

A desktop image with your own user and session:

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
RUN useradd --create-home --shell /bin/bash me
COPY supervisord.conf /etc/supervisor/conf.d/desktop.conf
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/supervisord", "-n"]
```

The same with OBS: use `FROM ghcr.io/iarnvidr/obs-runtime:32.2.0` instead.

Run guacd next to your Guacamole web app:

```sh
docker run -d --name guacd -p 4822:4822 ghcr.io/iarnvidr/guacd:1.6.0
```

Add the Tail browser bridge to any image:

```dockerfile
COPY --from=ghcr.io/iarnvidr/tail-browser-mcp:2.0.23 / /
```

### A virtual camera and microphone for call apps

Run OBS in the desktop (or anywhere on the network), compose what you want to show, and let any
call application in the remote desktop use it as its camera and microphone. The pieces:

- `desktop-base` (or `obs-runtime`) is the desktop: xrdp, XFCE, PipeWire, ffmpeg.
- `media-bridge` receives OBS's MPEG-TS/UDP output and publishes it as a v4l2loopback camera and a
  PipeWire microphone. The camera device is created on the host by `media-bridge/host/setup-v4l2loopback.sh`
  and mapped into the container.
- `obs-control` (upcoming) will drive OBS from outside and start the stream the bridge listens for.

```dockerfile
FROM ghcr.io/iarnvidr/obs-runtime:32.2.0
COPY --link --from=ghcr.io/iarnvidr/media-bridge:0.1.0 / /
# obs-control will be added here the same way when it is published
COPY supervisord.conf /etc/supervisor/conf.d/desktop.conf   # your programs, see media-bridge/share/
```

```sh
sudo media-bridge/host/setup-v4l2loopback.sh --label 'Desk camera'   # once, on the host
docker run --device /dev/video10 --group-add "$(stat -c %g /dev/video10)" -p 3389:3389 -p 5004:5004/udp my-desktop
```

See [media-bridge/README.md](media-bridge/README.md) for the settings and the file-output fallback
for hosts that cannot load the kernel module.

### Firefox

`desktop-base` ships Mozilla's official Firefox ESR unmodified, with no policies, extensions or
preferences. Customise it in your own image, for example with
`/opt/malrunar/firefox/distribution/policies.json`. A customised Firefox is a modified build under
[Mozilla's trademark policy](https://www.mozilla.org/foundation/trademarks/distribution-policy/),
and that applies to your image, not this one.

## Tags

Each image gets two tags:

- `<version>`, the main upstream version, for example `guacd:1.6.0`. It moves when the image is
  rebuilt with newer pins.
- `h-<hash>`, a hash of everything the image is built from: its recipe, its pins and its parent
  image. The same hash means the same inputs, so an `h-` tag is never rebuilt or moved.

## How the images are built

Every input is pinned: base images by digest, Ubuntu and Debian packages through a dated
[snapshot.ubuntu.com](https://snapshot.ubuntu.com/) or
[snapshot.debian.org](https://snapshot.debian.org/) archive, downloads by sha256 and git sources
by commit. The pins live in [`pins.env`](pins.env) and each image's `pins.env`. A weekly workflow
opens a pull request that moves them forward, which is how security updates arrive.

Builds run on GitHub-hosted runners: pull requests build and smoke-test every image, and pushes to
`main` publish them. Each published image has an SBOM, a provenance attestation and a keyless
[cosign](https://github.com/sigstore/cosign) signature. To check the signature:

```sh
cosign verify ghcr.io/iarnvidr/guacd@sha256:<digest> \
  --certificate-identity https://github.com/iarnvidr/malrunar/.github/workflows/publish.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Building locally

You need Docker with buildx. Build `desktop-base` before `obs-runtime`.

```sh
scripts/build.sh guacd --load && scripts/smoke.sh guacd
scripts/build.sh desktop-base --load && scripts/build.sh obs-runtime --load
scripts/build.sh media-bridge --load && scripts/smoke.sh media-bridge   # the smoke test needs desktop-base built
python3 scripts/malrunar_image.py tags guacd    # the tags this checkout would publish
```

To add or change an image, see [CONVENTIONS.md](CONVENTIONS.md).

## Licences

The build recipes are Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). The images contain
third-party software under its own licences, listed in [THIRD_PARTY.md](THIRD_PARTY.md). Among them
are OBS Studio (GPL-2.0), the Chromium Embedded Framework bundled with it, FreeRDP and Guacamole
(Apache-2.0), Firefox (MPL-2.0) and Tail (MIT). `media-bridge` adds only scripts. The images keep each Ubuntu package's copyright file.
The exact OBS package and its source are attached to the `obs-<debian version>` release (with `~` written as `-`).
