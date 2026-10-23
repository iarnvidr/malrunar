# thunderbird-mcp

The [Thunderbird MCP](https://github.com/TKasperczyk/thunderbird-mcp) bridge and add-on (MIT) as a
component: an MCP server over stdio that lets an AI agent read and search mail, contacts and
calendars, and draft messages, in a running Thunderbird. The add-on runs inside Thunderbird and serves
a local HTTP endpoint with a token; the bridge (`mcp-bridge.cjs`) turns MCP on stdin/stdout into requests
to it.

The component is `FROM scratch` and holds the bridge exactly as upstream publishes it at a pinned
commit, the add-on packed from the same commit, and a small wrapper. **It contains no Thunderbird and
does not change one.** Installing the add-on into a Thunderbird is your step (a policy fragment is
provided); the [`thunderbird`](../thunderbird/) component is one Thunderbird it works with, any
Thunderbird 102 or newer does too.

## Dependencies

| Needs | Why |
|---|---|
| [`node`](../node/) | the bridge is a Node program (version 18 or newer). Without it the wrapper exits with status 78 and a message naming it |
| a Thunderbird with the add-on installed | the bridge forwards to the add-on; with none running it answers `initialize` and reports every other request as an error that says Thunderbird is not reachable |

It does **not** need `desktop-base`, and it does not need the `thunderbird` component to be in the same
image: the bridge may run in another container that shares Thunderbird's temp directory, or on another
host that can reach the endpoint through a connection file. It needs no other component. The smoke test
enforces the dependency: in the bare pinned Ubuntu image the wrapper exits 78 naming `node`, and the
bridge's tests run in an image that holds the Ubuntu base, the `node` component and this component, nothing more.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/thunderbird:158.0.1 / /
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
COPY --link --from=ghcr.io/iarnvidr/thunderbird-mcp:0.7.4 / /
# Installing the add-on is a change to Thunderbird (see ../thunderbird/README.md, "What is modified, and where")
RUN python3 /opt/malrunar/thunderbird/consumer/customize.py policies \
        /opt/malrunar/thunderbird-mcp/share/policies-extension.json.example
```

Point your MCP client at the wrapper, which finds `node` on `PATH` or in `/opt/malrunar/node/bin`:

```json
{ "mcpServers": { "thunderbird": { "command": "/opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp" } } }
```

(`share/mcp-client.json.example`.) Run the bridge as the user that runs Thunderbird: the add-on writes
its connection file (port and token, mode 600) under that user's temporary directory, and the bridge looks
for it there. Nothing is added to `PATH` and the component starts no program of its own, so it has no
supervisord fragment: an MCP client starts the bridge when it needs it.

| Path | What it is |
|---|---|
| `/opt/malrunar/thunderbird-mcp/bin/malrunar-thunderbird-mcp` | The wrapper: checks node, maps the settings, runs the bridge. `--check` only checks |
| `/opt/malrunar/thunderbird-mcp/mcp-bridge.cjs`, `package.json` | Upstream's bridge, unmodified |
| `/opt/malrunar/thunderbird-mcp/thunderbird-mcp.xpi` | The add-on, id `thunderbird-mcp@tkasperczyk.dev` |
| `/opt/malrunar/thunderbird-mcp/share/` | `policies-extension.json.example` (installs the add-on), `mcp-client.json.example` |
| `/opt/malrunar/thunderbird-mcp/UPSTREAM-LICENSE`, `LICENSE`, `SOURCE.txt` | The upstream MIT licence; this repository's Apache-2.0 licence for the wrapper and recipe; the exact source |

## Configuration

All variables are optional.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_THUNDERBIRD_MCP_NODE` | `node` from `PATH`, else `/opt/malrunar/node/bin/node` | The node to run the bridge with |
| `MALRUNAR_THUNDERBIRD_MCP_CONNECTION_FILE` | discovered | The add-on's `connection.json`, when it is not under the default temporary directory (becomes the bridge's `THUNDERBIRD_MCP_CONNECTION_FILE`) |
| `MALRUNAR_THUNDERBIRD_MCP_DEBUG` | `0` | `1` makes the bridge log to stderr |

The add-on itself is configured in Thunderbird (its options page), as upstream documents.

### The add-on's inline-image limits

Upstream compiles `1 MiB` per image and `4 MiB` in total (base64) into the add-on, which skips ordinary
photos and screenshots with no setting to change it. This component's add-on is packed from the pinned
source with larger limits (`MAX_INLINE_IMAGE_BASE64_BYTES` and `MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES` in
[`pins.env`](pins.env), 96 MiB and 384 MiB of base64 by default); the tool description and the reply report
the active limits and any skipped image. The add-on's version is `<upstream>.<number derived from the
limits>`, so a persistent profile installs it over an older build. The limits are build-time values: to use
others, rebuild this component with your own `pins.env` values, or install upstream's add-on instead.
Images are only inlined when the client asks (`includeInlineImages`), and a large total costs agent context.

## Safety

The add-on exposes a mailbox to an MCP client. Upstream's safeguards are kept as they are: the bearer token
in the connection file, a refusal to attach files from sensitive locations, and the review window before a
message is sent. Installing the add-on with `normal_installed` (as the policy fragment does) leaves it
removable from Thunderbird's add-ons page. Run only agents you trust against a real mailbox.
