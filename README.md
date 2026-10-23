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
| [`obs-control`](obs-control/) | OBS you drive over HTTP | `obs-runtime` + a controller: OBS starts headless with a scene collection generated from a config file, and an HTTP API switches scenes, serves program previews, plays and records clips, reports input levels, controls the output (MPEG-TS over UDP) and makes automatic b-roll. Plugins add routes and scenes. See [docs/obs-control-api.md](docs/obs-control-api.md) |
| [`guacd`](guacd/) | The Guacamole proxy daemon, as a container | guacd 1.6.0 built against FreeRDP 3, with an RDP GDI fix, listening on port 4822 |
| [`guacamole-tunnel`](guacamole-tunnel/) | Several viewers in ONE remote desktop session | A small proxy in front of the Guacamole tunnel endpoints: a user's second viewer joins their running session instead of evicting the first. Its own container; it does not touch `guacd` |
| [`tail-browser-mcp`](tail-browser-mcp/) | Letting an AI agent drive Firefox | The [Tail](https://github.com/rtf6x/tail-browser-mcp) MCP server, client and Firefox extension in `/opt/malrunar/tail-browser-mcp`. A `FROM scratch` layer you copy into your image |
| [`office-convert`](office-convert/) | Turning Office documents into PDF | A small HTTP service around LibreOffice headless (Impress and Writer filters only, Liberation and Carlito fonts): `POST /convert` takes a .pptx, .ppt, .odp, .key, .docx, .doc or .odt and returns a PDF. One conversion at a time, no state, runs as uid 65534. It has no authentication: keep it on a trusted network. See [office-convert/README.md](office-convert/README.md) |
| [`media-bridge`](media-bridge/) | An OBS-driven virtual camera and microphone for any call app in the desktop | A receiver that turns an MPEG-TS/UDP stream into a v4l2loopback camera and a PipeWire microphone, plus host setup, supervisord/systemd examples and audio/browser helpers in `/opt/malrunar/media-bridge`. A `FROM scratch` layer you copy into your image |
| [`node`](node/) | A Node.js runtime in any image | Node.js 24 LTS, the unmodified nodejs.org release, in `/opt/malrunar/node`. A `FROM scratch` layer you copy into your image; it depends on nothing |
| [`work-browser`](work-browser/) | A persistent Firefox for the desktop user | A launcher, an open-URL helper and a supervisord program for the unmodified Firefox of `desktop-base`, with the profile, preferences and policies supplied by you. A `FROM scratch` layer; it needs `desktop-base` only |
| [`session-kit`](session-kit/) | Desktop session tools: workspaces, window placement, session check, default-browser launchers | `malrunar-session` and a supervisord program in `/opt/malrunar/session-kit`: names the XFCE workspaces and places windows on them, restores a Firefox profile's tabs, checks the session and registers the desktop's web browser. A `FROM scratch` layer; it needs `desktop-base` (and `xdotool`, installed by you) and uses the work browser only when that component is present |
| [`thunderbird`](thunderbird/) | A mail client in the desktop | Mozilla's official Thunderbird 158.0.1, **unmodified** (the release tarball, checked against Mozilla's signed checksums), and a supervisord program that keeps it running on a persistent profile. The changes some setups need (an OAuth patch for shared mailboxes, add-ons, policies) are scripts you run at your own build, in `thunderbird/consumer/`. A `FROM scratch` layer; it needs `desktop-base` only |
| [`thunderbird-mcp`](thunderbird-mcp/) | Letting an AI agent work in the mailbox | The [Thunderbird MCP](https://github.com/TKasperczyk/thunderbird-mcp) bridge and add-on in `/opt/malrunar/thunderbird-mcp`, with a wrapper. A `FROM scratch` layer with no Thunderbird in it; it needs the `node` component and talks to a Thunderbird that has the add-on installed |

## Using them

Pin by digest in anything you deploy. Tags are listed under [Tags](#tags).

A desktop image with your own user and session:

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
RUN useradd --create-home --shell /bin/bash me
COPY supervisord.conf /etc/supervisor/conf.d/desktop.conf
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/supervisord", "-n"]
```

The same with OBS: use `FROM ghcr.io/iarnvidr/obs-runtime:32.2.0` instead. For OBS driven over HTTP,
run `ghcr.io/iarnvidr/obs-control` as it is (see below).

Run guacd next to your Guacamole web app:

```sh
docker run -d --name guacd -p 4822:4822 ghcr.io/iarnvidr/guacd:1.6.0
```

Add the Tail browser bridge to any image (its server is a Node bundle: add the `node` component too):

```dockerfile
COPY --from=ghcr.io/iarnvidr/tail-browser-mcp:2.0.23 / /
COPY --from=ghcr.io/iarnvidr/node:24.22.1 / /
```

### A virtual camera and microphone for call apps

Run OBS in the desktop (or anywhere on the network), compose what you want to show, and let any
call application in the remote desktop use it as its camera and microphone. The pieces:

- `desktop-base` (or `obs-runtime`) is the desktop: xrdp, XFCE, PipeWire, ffmpeg.
- `media-bridge` receives OBS's MPEG-TS/UDP output and publishes it as a v4l2loopback camera and a
  PipeWire microphone. The camera device is created on the host by `media-bridge/host/setup-v4l2loopback.sh`
  and mapped into the container.
- [`obs-control`](docs/obs-control-api.md) runs OBS headless and drives it over HTTP: it generates the scenes from a config file, sends the program as MPEG-TS/UDP to the bridge, and serves the API that switches scenes, plays clips and starts the stream.

```dockerfile
# the desktop: it only receives the stream (OBS runs in the obs-control container, below)
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/media-bridge:0.1.0 / /
COPY supervisord.conf /etc/supervisor/conf.d/desktop.conf   # your programs, see media-bridge/share/
```

```sh
sudo media-bridge/host/setup-v4l2loopback.sh --label 'Desk camera'   # once, on the host
docker run --device /dev/video10 --group-add "$(stat -c %g /dev/video10)" -p 3389:3389 -p 5004:5004/udp my-desktop
```

See [media-bridge/README.md](media-bridge/README.md) for the settings and the file-output fallback
for hosts that cannot load the kernel module.

Running OBS next to the desktop, with `obs-control`:

```yaml
# compose.yaml
services:
  desktop:                      # your image: FROM desktop-base, COPY --link --from=...media-bridge / /
    build: ./desktop            # and a supervisord program running /opt/malrunar/media-bridge/bin/malrunar-media-bridge
    devices: ["/dev/video10:/dev/video10"]        # the v4l2loopback camera, created on the host first
    environment:
      MALRUNAR_BRIDGE_START_OUTPUT_URL: http://obs:18095/api/start-output   # the bridge asks OBS to start streaming
  obs:
    image: ghcr.io/iarnvidr/obs-control:0.1.0
    shm_size: 512m
    environment:
      MALRUNAR_OBS_CAMERA_URL: https://example.invalid/camera-page          # what the browser source shows
      MALRUNAR_OBS_OUTPUT_URL: udp://desktop:5004?pkt_size=1316             # where the bridge listens
    volumes: ["obs-config:/config"]
    ports: ["127.0.0.1:18095:18095"]                                          # the API and console
volumes:
  obs-config:
```

```sh
curl -fsS http://127.0.0.1:18095/ready                       # OBS is up
curl -fsS -X POST http://127.0.0.1:18095/api/scene/live      # put the camera page on air
curl -fsS -o preview.jpg http://127.0.0.1:18095/api/program-preview.jpg
```

Configuration is a TOML file or `MALRUNAR_OBS_*` variables; the API, the compatibility table with
the controller it came from, and the plugin interface are in
[docs/obs-control-api.md](docs/obs-control-api.md). The end-to-end test that runs exactly this chain
(OBS to the bridge, with audio checked at the virtual microphone) is `obs-control/test/e2e/`.

### Firefox

`desktop-base` ships Mozilla's official Firefox ESR unmodified, with no policies, extensions or
preferences. Customise it in your own image, for example with
`/opt/malrunar/firefox/distribution/policies.json`. A customised Firefox is a modified build under
[Mozilla's trademark policy](https://www.mozilla.org/foundation/trademarks/distribution-policy/),
and that applies to your image, not this one.

### Thunderbird

The same holds for [`thunderbird`](thunderbird/): it is Mozilla's latest stable release as Mozilla builds it.
Policies, add-ons and the OAuth patch for shared mailboxes are not in the image; `thunderbird/consumer/`
has the scripts (and a Dockerfile snippet in its README) that you run in your own build, and CI applies them
to a copy of the unmodified release in a throwaway image that is never published.

## Apps

Not everything here is an image. [`apps/desktop-wrapper`](apps/desktop-wrapper/) is a macOS desktop
app (Tauri) that wraps one website in a window of its own, with native notifications; its name,
address, bundle id and icon come from a configuration file. It is built by its own workflow
(`.github/workflows/desktop-wrapper.yml`) and is not published as an image.

## Composing a desktop

A product image is `desktop-base` plus an explicit list of components. Every component is opt-in:
none pulls in another, each says in its README what it needs, and its smoke test checks that. Two
examples of how the parts combine (no product is implied; take the rows you need):

| Component | Kind | Needs | A calls desktop | A work desktop |
|---|---|---|---|---|
| [`desktop-base`](desktop-base/) | base image (`FROM`) | nothing | yes | yes |
| [`obs-runtime`](obs-runtime/) | base image (`FROM`, instead of `desktop-base`) | `desktop-base` | only if OBS runs in the desktop | no |
| [`obs-control`](obs-control/) | its own container | nothing | yes, next to the desktop | no |
| [`guacd`](guacd/) | its own container | nothing | yes, with a Guacamole web app | yes, with a Guacamole web app |
| [`guacamole-tunnel`](guacamole-tunnel/) | its own container | a Guacamole web app and a proxy that sets an identity header (no component of this repository) | optional, when several people watch the same desktop | optional |
| [`office-convert`](office-convert/) | its own container | nothing | no | optional, next to the desktop |
| [`media-bridge`](media-bridge/) | component (`COPY --from`) | `desktop-base` | yes | no |
| [`node`](node/) | component (`COPY --from`) | nothing | optional | optional |
| [`tail-browser-mcp`](tail-browser-mcp/) | component (`COPY --from`) | `node` at run time | optional | optional |
| [`work-browser`](work-browser/) | component (`COPY --from`, with a supervisord program) | `desktop-base` | optional | yes |
| [`session-kit`](session-kit/) | component (`COPY --from`, with a supervisord program) | `desktop-base`, `xdotool` for window tools; `work-browser` optional | yes (workspaces, session check) | yes (with `work-browser`: reopen tabs, default browser) |
| [`thunderbird`](thunderbird/) | component (`COPY --from`, with a supervisord program) | `desktop-base` | no | optional (a mail client) |
| [`thunderbird-mcp`](thunderbird-mcp/) | component (`COPY --from`) | `node` at run time; a Thunderbird with its add-on installed (for example `thunderbird`) | no | optional (agent access to the mailbox) |

```dockerfile
# a calls desktop: it receives the stream OBS (in its own container) sends, for a call app to use
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/media-bridge:0.1.0 / /
# your user, session and supervisord programs on top

# a work desktop: a persistent Firefox and nothing else
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/work-browser:0.1.0 / /
# your user (MALRUNAR_WORK_BROWSER_USER), policies and preferences on top, see work-browser/README.md

# a work desktop with a mail client an agent can use: the components you ask for and nothing more
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/thunderbird:158.0.1 / /
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
COPY --link --from=ghcr.io/iarnvidr/thunderbird-mcp:0.7.4 / /
# your user (MALRUNAR_THUNDERBIRD_USER) and, if you want the add-on installed, the policy fragment, see thunderbird-mcp/README.md
```

Components that run a program as part of the desktop (`work-browser`, `thunderbird`) ship it as a supervisord
fragment, `/etc/supervisor/conf.d/malrunar-<component>.conf`, which is in the image exactly when the
component was copied in. The convention is in [CONVENTIONS.md](CONVENTIONS.md#supervisord-programs-of-components).
Add `node` next to anything that runs JavaScript, such as `tail-browser-mcp`:

```dockerfile
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
ENV PATH=/opt/malrunar/node/bin:$PATH
```

## Build and deploy tools

[`tools/`](tools/README.md) holds the generic tooling used to build and ship composed images:
parent-chained content hashing for any Dockerfile build graph (`scripts/malrunar_image.py graph`),
delivery of an image to a Docker host that already has most of its layers (pinned `docker pussh`
with a `save`/`load` fallback), assembly of a base plus an explicit component list, and an image
equivalence comparison. Each tool is opt-in and works on its own; registries, hosts and image lists
come from arguments or a manifest. They are not images, so they are not in the table above.

## Reviewed and not included

While building these images, the session scripts of a call desktop were reviewed and deliberately
left out: they are specific to one deployment (a fixed user name, fixed paths, secrets handling and
Firefox policies tied to one set of sites). Their generic parts are already covered by `desktop-base`
and `obs-runtime`; the rest belongs in your own image on top of them.

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
opens a pull request that moves them forward (and lists which images it republishes), which is how
security updates arrive.

Builds run on GitHub-hosted runners: pull requests build and smoke-test the images they change, and
pushes to `main` publish them. An image's hash covers only the pins its Dockerfile uses, so the weekly
pin update republishes only the images it actually changes. Each published image has an SBOM, a provenance attestation and a keyless
[cosign](https://github.com/sigstore/cosign) signature. To check the signature:

```sh
cosign verify ghcr.io/iarnvidr/guacd@sha256:<digest> \
  --certificate-identity https://github.com/iarnvidr/malrunar/.github/workflows/publish.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Building locally

You need Docker with buildx. Build `desktop-base` before `obs-runtime` and `obs-runtime` before `obs-control`.

```sh
scripts/build.sh guacd --load && scripts/smoke.sh guacd
scripts/build.sh desktop-base --load && scripts/build.sh obs-runtime --load && scripts/build.sh obs-control --load && scripts/smoke.sh obs-control
scripts/build.sh media-bridge --load && scripts/smoke.sh media-bridge   # the smoke test needs desktop-base built
scripts/build.sh office-convert --load && scripts/smoke.sh office-convert
scripts/build.sh guacamole-tunnel --load && scripts/smoke.sh guacamole-tunnel
scripts/build.sh node --load && scripts/smoke.sh node
scripts/build.sh work-browser --load && scripts/smoke.sh work-browser   # the smoke test needs desktop-base built
scripts/build.sh node --load && scripts/build.sh thunderbird --load && scripts/smoke.sh thunderbird   # needs desktop-base and node built
scripts/build.sh thunderbird-mcp --load && scripts/smoke.sh thunderbird-mcp   # needs desktop-base, node and thunderbird built
python3 -m unittest discover -s scripts/test    # hashes and pull request planning, and the tests of tools/
python3 scripts/malrunar_image.py tags guacd    # the tags this checkout would publish
```

To add or change an image, see [CONVENTIONS.md](CONVENTIONS.md).

## Licences

The build recipes are Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). The images contain
third-party software under its own licences, listed in [THIRD_PARTY.md](THIRD_PARTY.md). Among them
are OBS Studio (GPL-2.0), the Chromium Embedded Framework bundled with it, FreeRDP and Guacamole
(Apache-2.0), Firefox and Thunderbird (MPL-2.0), Tail and thunderbird-mcp (MIT). `media-bridge` and `work-browser` add only scripts; `thunderbird` adds Mozilla's unmodified Thunderbird (MPL-2.0) and scripts, and `thunderbird-mcp` adds the upstream bridge and add-on (MIT); `node` adds the unmodified Node.js release (MIT); `office-convert` adds LibreOffice (MPL-2.0 and others) and its own Python script; `obs-control` adds Python code of its own (Apache-2.0) and aiohttp and websockets. The images keep each Ubuntu package's copyright file.
The exact OBS package and its source are attached to the `obs-<debian version>` release (with `~` written as `-`).
