# node

The official Node.js LTS runtime as a component image. Add it to any image that needs `node`, for
example to run an MCP server, a bridge written in JavaScript, or `npm ci` at build time.

The image is `FROM scratch` and holds only `/opt/malrunar/node`: the unmodified nodejs.org linux-x64
release (`bin/node`, `bin/npm`, `bin/npx`, `bin/corepack`, `lib/node_modules`, `LICENSE`), with the
upstream headers, man pages and changelog removed, and `SOURCE.txt` recording the download URL and
sha256 it was verified against.

## Dependencies

None. `node` is a self-contained binary that needs only the C library of the image it is copied
into (any glibc-based image, including `desktop-base`). It does not need `desktop-base`, and no other
component needs to be present for it to work. It starts nothing and ships no supervisord fragment.
The smoke test runs it in the bare pinned Ubuntu image to keep that true.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr          # or any other base
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
ENV PATH=/opt/malrunar/node/bin:$PATH
```

Nothing is added to `PATH` and no symlink is created outside `/opt/malrunar/node`; the `ENV` line
above (or calling `/opt/malrunar/node/bin/node` by its path, for example in a supervisord program)
is yours to choose. `npm`, `npx` and `corepack` are in the same `bin/` directory.

Other components that run JavaScript (for example `tail-browser-mcp`, whose server is a bundle that
needs `node` and nothing else) do not carry a runtime of their own: add this component next to them.

## Configuration

The component reads no environment variables and has no settings; it is the runtime itself.

## Version

The tag `<version>` is the Node.js version (`24.22.1`), from `pins.env`. It follows the 24.x LTS line:
the weekly pin update (`scripts/bump_pins.py`) moves `NODE_VERSION` and `NODE_SHA256` to the newest
24.x LTS release and the sha256 listed in that release's `SHASUMS256.txt`. Moving to another major
line is a deliberate change of `pins.env`.

## Licence

Node.js is under the MIT licence with bundled third-party components, all listed in
`/opt/malrunar/node/LICENSE`. The recipe is Apache-2.0 like the rest of this repository.
