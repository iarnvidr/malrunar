# Third-party software

The recipes in this repository are Apache-2.0 (see LICENSE and NOTICE). The images contain the
third-party software below under its own licences. Versions are those pinned in `pins.env` and
`<image>/pins.env`; the authoritative per-package licence text is in the image at
`/usr/share/doc/<package>/copyright` (kept in every image) and, for components installed outside
dpkg, under `/usr/local/share/malrunar/licenses/<component>/`.

This file is a summary, not legal advice. Check the images themselves when you redistribute them.

## Ubuntu packages (desktop-base, obs-runtime, obs-control, guacd, guacamole-tunnel, office-convert)

All packages installed through apt come from the Ubuntu 26.04 archive (main, universe, restricted,
multiverse) at the snapshot timestamp `APT_SNAPSHOT` in `pins.env`, with their upstream licences
(GPL, LGPL, MIT, BSD, MPL and others as recorded in each package's `copyright` file). The package
list is in each Dockerfile and, exactly, in the image SBOM attached to every published image.

- Source for every Ubuntu package: `https://snapshot.ubuntu.com/ubuntu/<APT_SNAPSHOT>/` (the
  `Sources` indexes and source files of the same snapshot), or `apt-get source <package>` against
  that snapshot, or https://launchpad.net/ubuntu/+source/<source-package>.
- Notable GPL/LGPL components in `desktop-base`: xrdp, xorgxrdp (Apache-2.0 / MIT / GPL parts, see
  copyright files), XFCE (GPL-2.0+/LGPL-2.1+), PipeWire and WirePlumber (MIT), ffmpeg (Ubuntu's
  build is GPL-enabled: GPL-2.0+ with LGPL-2.1+ libraries), supervisor (BSD-style), tini (MIT).
- `libx264` (GPL-2.0+) and `libfdk-aac2` (Fraunhofer FDK AAC licence, from `multiverse`) are pulled
  in by the OBS package. The FDK AAC licence grants no patent licence; users are responsible for
  any patent licences their use requires.

## OBS Studio (obs-runtime): GPL-2.0-or-later

The `obs-studio` .deb from the OBS Project PPA (ppa:obsproject/obs-studio), pinned by sha256 in
`obs-runtime/pins.env`. OBS Studio is licensed under the GNU General Public License v2.0 or later;
the licence text is in `/usr/share/doc/obs-studio/copyright` in the image.

Source offer: the corresponding source is the Debian source package of the same version
(`.dsc`, `orig` and `debian` tarballs), upstream at https://github.com/obsproject/obs-studio and
on Launchpad at https://launchpad.net/~obsproject/+archive/ubuntu/obs-studio. Because the PPA keeps
only the latest version, the publish workflow attaches the exact binary package and its source,
with `SHA256SUMS`, to a GitHub release `obs-<version>` of this repository. The same files can be
produced at any time with `obs-runtime/fetch-source.sh <output-dir>`, which verifies them against
the sha256 values pinned in `obs-runtime/pins.env`. This repository does not modify OBS Studio.

### CEF (Chromium Embedded Framework): BSD-3-Clause, with Chromium third-party notices

The OBS package bundles CEF as `obs-plugins/libcef.so`, used by obs-browser (browser sources). The
exact version is recorded in `obs-runtime/cef-version.env` (also installed in the image at
`/usr/local/share/malrunar/obs-runtime/cef-version.env`):

- CEF `127.0.0-6533-fix-stutter-and-osr-extra-info.3042+g176b09c+chromium-127.0.6533.120`, built
  2025-08-16, source https://github.com/tytan652/cef.git commit `176b09c19dec2b37c74b7e8a666b33e3a014094b`
  (a fork of https://bitbucket.org/chromiumembedded/cef used by OBS).
- Chromium `127.0.6533.120`, source https://chromium.googlesource.com/chromium/src.git commit
  `c51f2916e4a14157c276fd33c8bfb24155aac1ad`.

The CEF licence (BSD-3-Clause) is installed at `/usr/local/share/malrunar/licenses/cef/LICENSE.txt`.
Chromium and the libraries compiled into `libcef.so` carry their own licences (BSD-3-Clause, MIT,
Apache-2.0, MPL-2.0, LGPL-2.1+, ISC, Zlib and others). The OBS package ships no `about:credits`
page, so the notices are those of the Chromium source at the commit above: the top-level
`LICENSE` at
https://chromium.googlesource.com/chromium/src.git/+/c51f2916e4a14157c276fd33c8bfb24155aac1ad/LICENSE
and the `LICENSE`/`README.chromium` files in its `third_party/` tree. LGPL components of Chromium
(for example ffmpeg inside Chromium) are available as source from the Chromium repository above.

## guacd (guacamole-server and FreeRDP): Apache-2.0

- Apache Guacamole `guacamole-server` 1.6.0, Apache-2.0, https://guacamole.apache.org/, source
  https://archive.apache.org/dist/guacamole/1.6.0/source/. Built from the verified release tarball
  with two files replaced by newer upstream revisions (`wol.c`, `socket-nest.c`, commits in
  `guacd/pins.env`) and the RDP GDI patch `guacd/guacamole-rdp-gdi.patch`. The patch, licence, NOTICE
  and a `SOURCE.txt` are installed at `/usr/local/share/malrunar/licenses/guacamole-server/`.
- FreeRDP 3 (`freerdp3`, `libwinpr3`) from Ubuntu, Apache-2.0, https://www.freerdp.com/ (source:
  Ubuntu source package `freerdp3`).
- Runtime libraries (cairo, libjpeg, libpng, libwebp, OpenSSL, PulseAudio client and others) from
  Ubuntu with their own licences (LGPL/MPL, IJG/BSD, libpng, BSD, Apache-2.0, LGPL-2.1+); their
  `copyright` files are kept in the image.

## obs-control: the controller and its Python libraries

The obs-control code (`obs-control/malrunar_obs`, the example configuration and the test card) is
Apache-2.0 like the rest of this repository. It is run by the `python3` of `desktop-base` and uses
two libraries installed from the pinned Ubuntu snapshot, with their dependencies and their
`copyright` files:

- `python3-aiohttp` (Apache-2.0) and its dependencies (`python3-multidict`, `python3-yarl`,
  `python3-frozenlist`, `python3-aiosignal`, `python3-attr`, `python3-propcache` and others, each under its own
  permissive licence).
- `python3-websockets` (BSD-3-Clause).

It drives OBS Studio through obs-websocket, which is part of the OBS Studio package above
(GPL-2.0-or-later). The controller talks to OBS over a local WebSocket and links none of its code. The
automatic b-roll and clip features run the `ffmpeg` and `ffprobe` of `desktop-base`.

## Firefox ESR (desktop-base): MPL-2.0

Mozilla's official, unmodified Firefox ESR Linux build (`FIREFOX_ESR_VERSION` in
`desktop-base/pins.env`), downloaded from https://ftp.mozilla.org/pub/firefox/releases/ and checked
against a pinned sha256. Firefox is licensed under the Mozilla Public License 2.0 together with
the licences of the components it bundles (see `about:license` inside Firefox). Source code for
the exact release: https://ftp.mozilla.org/pub/firefox/releases/<version>/source/ and
https://hg.mozilla.org/releases/mozilla-esr153/. This repository does not modify Firefox. Firefox
and the Firefox logo are Mozilla trademarks; see the README for what that means for derived images.

## pipewire-module-xrdp (desktop-base): MIT

https://github.com/neutrinolabs/pipewire-module-xrdp at the commit pinned in
`desktop-base/pins.env`, built from its GitHub tarball. Licence at
`/usr/local/share/malrunar/licenses/pipewire-module-xrdp/LICENSE`.

## Tail browser MCP (tail-browser-mcp): MIT

Tail, https://github.com/rtf6x/tail-browser-mcp, version 2.0.23 at the commit pinned in
`tail-browser-mcp/pins.env` (MIT, copyright its authors; licence in the image as
`/opt/malrunar/tail-browser-mcp/TAIL-LICENSE`). The image holds its bundled MCP server and client
and the Firefox extension. The npm dependencies compiled into the bundles keep their licences
(MIT, ISC, BSD, Apache-2.0); the complete list with licence texts is generated at build time into
`/opt/malrunar/tail-browser-mcp/THIRD-PARTY-LICENSES.txt`, and `SOURCE.txt` records the source
commit.

## Thunderbird (thunderbird): MPL-2.0

Mozilla's official, unmodified Thunderbird Linux build (`THUNDERBIRD_VERSION` in `thunderbird/pins.env`),
downloaded from https://archive.mozilla.org/pub/thunderbird/releases/ and checked against the release's
PGP-signed `SHA256SUMS` (signed by a subkey of the "Mozilla Software Releases" key, fingerprint
`14F26682D0916CDD81E37B6D61B7B526D98F0353`, key file `thunderbird/mozilla-release-keys.asc`) and against
the pinned sha256. Thunderbird is licensed under the Mozilla Public License 2.0 together with the licences of
the components it bundles (see `about:license` inside Thunderbird). The licence text is in the image as
`/opt/malrunar/thunderbird/LICENSE-MPL-2.0` and the exact source is named in
`/opt/malrunar/thunderbird/SOURCE.txt`: https://archive.mozilla.org/pub/thunderbird/releases/<version>/source/
and https://hg.mozilla.org/releases/comm-release/. This repository does not modify Thunderbird in any image.
Thunderbird and its logo are Mozilla trademarks; see the README for what that means for derived images.

Everything under `thunderbird/consumer/` runs only in a consumer's own build and is never applied to a published
image. `oauth_shared_mailbox.py` contains replacement text for Mozilla's `OAuth2Module.sys.mjs` (an MPL-2.0
file) and is offered under MPL-2.0; the rest of the directory is Apache-2.0. `addons.json` pins two third-party
add-ons that the consumer tool can download from addons.thunderbird.net: TbSync (MPL-2.0, John Bieling) and
Provider for Exchange ActiveSync (MPL-2.0, John Bieling). They are not in any image of this repository.

## Thunderbird MCP (thunderbird-mcp): MIT

thunderbird-mcp, https://github.com/TKasperczyk/thunderbird-mcp, version 0.7.4 at the commit pinned in
`thunderbird-mcp/pins.env` (MIT, copyright 2025 Tomasz Kasperczyk; licence in the image as
`/opt/malrunar/thunderbird-mcp/UPSTREAM-LICENSE`, `SOURCE.txt` records the commit). `mcp-bridge.cjs` and
`package.json` are included as upstream publishes them. The add-on `thunderbird-mcp.xpi` is packed from the
same commit with two constants changed (the inline-image limits) and its version number extended; the
wrapper `bin/malrunar-thunderbird-mcp` and the recipe are Apache-2.0. The bridge runs under the `node`
component and uses no npm dependencies. The component contains no Thunderbird.

## Build-time only (not shipped in the images)

Node.js (MIT, official `node` image), esbuild (MIT), gcc/make/autotools and other build tools from
Debian/Ubuntu, and gnupg, xz-utils, git and python3 in the build stages of `thunderbird` and `thunderbird-mcp`. Container base images: `ubuntu` (various licences, per-package copyright files) and
`node` (Debian bookworm-slim + Node.js).

## media-bridge

`media-bridge` is Apache-2.0 and contains scripts only. At run time it uses ffmpeg, `pactl`
(pulseaudio-utils), PipeWire and python3 from `desktop-base` under the licences listed above. It does
not ship the `v4l2loopback` kernel module (GPL-2.0), which you install on the host yourself
(`host/setup-v4l2loopback.sh` only configures it). CI installs the distribution's
`v4l2loopback-dkms` package on a GitHub-hosted runner to test the camera path; that package is not
part of any image.

## office-convert

`office-convert` is Apache-2.0 for its own code (`office-convert/convert.py`, standard library only).
It installs from the pinned Ubuntu snapshot the LibreOffice Impress and Writer packages without a GUI
(`libreoffice-impress-nogui`, `libreoffice-writer-nogui`, with their dependencies; LibreOffice is
MPL-2.0 with parts under LGPL and other licences, https://www.libreoffice.org/, source: Ubuntu
source package `libreoffice`), `python3` and `fontconfig`, and the fonts Liberation 2 (SIL OFL-1.1,
`fonts-liberation2`), Carlito and Caladea (SIL OFL-1.1, `fonts-crosextra-carlito`,
`fonts-crosextra-caladea`) and DejaVu (free Bitstream Vera/DejaVu licence, `fonts-dejavu-core`).
Each package's copyright file is kept in the image.

## guacamole-tunnel

`guacamole-tunnel` is Apache-2.0 for its own code (`guacamole-tunnel/guacamole_tunnel.py`). It installs from the
pinned Ubuntu snapshot `python3`, `python3-aiohttp` (Apache-2.0) and `python3-yarl` (Apache-2.0) with their
dependencies; each package's copyright file is kept in the image. It contains no Guacamole code.

## node

The `node` component is the unmodified Node.js linux-x64 release from https://nodejs.org/dist/
(version and sha256 in `node/pins.env`). Node.js is MIT-licensed and bundles V8 (BSD), OpenSSL
(Apache-2.0), ICU (Unicode licence), npm (Artistic-2.0), libuv, undici, zlib and others; the complete
licence file of the release is in the image as `/opt/malrunar/node/LICENSE`. Only the headers, man
pages and changelog are removed. Source: https://github.com/nodejs/node at the tag `v<version>`.

## work-browser

`work-browser` is Apache-2.0 and contains scripts only. At run time it uses the Firefox of
`desktop-base` (Mozilla, MPL-2.0), unmodified and not redistributed by this component, together with
`dbus-daemon`, `xdpyinfo` and `setpriv` from `desktop-base`.

## apps/desktop-wrapper (not part of any image)

The macOS app is Apache-2.0 (the recipe and its own code). It is built with Tauri v2 and its crates
(Apache-2.0 or MIT), `mac-notification-sys` (MIT), `notify-rust` (MIT or Apache-2.0, non-macOS builds
only) and the Tauri CLI (Apache-2.0 or MIT). The exact versions are in `apps/desktop-wrapper/src-tauri/Cargo.lock` and
`apps/desktop-wrapper/package-lock.json`; each crate's licence is in its own package.
