# teams-service

A local copy of your Microsoft Teams chats and calendar in [PocketBase](https://pocketbase.io/), kept in
sync through [msteams-mcp](../msteams-mcp/)'s own API modules, behind a small HTTP API for a web app or an
agent. Reads come from the local copy and work offline; writes (send, edit, react, delete, mark read,
calendar create/update/respond/delete) go live to Microsoft and fail visibly when the session is gone; nothing
is queued. It is a sibling of the `msteams-mcp` component rather than part of it: it has another
dependency profile, it ships a database and a long-lived program, and a Teams MCP does not need it.

It does not sign in. It reads the session that the `msteams-mcp` component signed in, lets that package's
own refresh logic renew the tokens, and **never writes the MCP's session files** (an in-process guard
turns every write to them into memory only; tests prove it against the real package).

## Dependencies

Declared here and checked by the program's script, which exits with status 78 and a message naming what is
missing. The smoke test runs the component in the bare Ubuntu image with exactly these and nothing else
(no `desktop-base`, no `work-browser`), and once more under supervisord on `desktop-base`.

| Needs | Why |
|---|---|
| [`node`](../node/) component | Runs the service (Node 22 or newer; no npm dependencies). Found at `/opt/malrunar/node/bin/node`, else on `PATH`, else `MALRUNAR_TEAMS_SERVICE_NODE` |
| [`msteams-mcp`](../msteams-mcp/) component (its package, not its command) | The service imports the patched API modules in `/opt/malrunar/msteams-mcp/app/dist` (`MALRUNAR_TEAMS_SERVICE_MCP_DIST`) and reads the session that component signed in |
| a signed-in session | `malrunar-msteams-mcp login` (or `login-browser`, or the optional `device-login`) as **the same user and host name**. Without one the service runs, reports `haveTokens: false`, and serves its (empty) copy |
| a user | Created in your own layer. The session key derives from the host name and user name, so this is the user that signed in (`MALRUNAR_TEAMS_SERVICE_USER`, else `MALRUNAR_MSTEAMS_MCP_USER`, else `MALRUNAR_WORK_BROWSER_USER`) |

For the supervisord program (below) the image also needs `supervisor`, which `desktop-base` has; run directly
(`malrunar-teams-service` in your own init) it needs nothing more than `node`, `bash` and `setpriv` (util-linux).

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
COPY --link --from=ghcr.io/iarnvidr/msteams-mcp:0.27.2 / /        # which itself needs work-browser to run its own commands
COPY --link --from=ghcr.io/iarnvidr/teams-service:0.1.0 / /
RUN useradd --create-home --uid 1000 --shell /bin/bash me
ENV PATH=/opt/malrunar/node/bin:$PATH MALRUNAR_TEAMS_SERVICE_USER=me
```

Copying the component in adds the supervisord program `teams-service`
(`/etc/supervisor/conf.d/malrunar-teams-service.conf`, the
[supervisor.d convention](../CONVENTIONS.md#supervisord-programs-of-components)). It runs
`/opt/malrunar/teams-service/bin/malrunar-teams-service`, which starts PocketBase (loopback only) and the
service and stops both when either one exits, so supervisord restarts the pair. The fragment names no user,
path or host. To turn the program off in one image delete the file in your layer or set
`MALRUNAR_TEAMS_SERVICE_ENABLED=0` (the script exits at once with status 0).

| Path | What it is |
|---|---|
| `/opt/malrunar/teams-service/bin/malrunar-teams-service` | The program's script (`run` by default; `check` prints what it found) |
| `/opt/malrunar/teams-service/bin/malrunar-teams-pocketbase` | PocketBase with the schema compiled in |
| `/opt/malrunar/teams-service/src/` | The service (Node, no npm dependencies) |
| `/opt/malrunar/teams-service/dev/` | A fixture server with synthetic chats (below) |
| `/etc/supervisor/conf.d/malrunar-teams-service.conf` | The supervisord program `teams-service` |

## Configuration

All optional. The service reads only `MALRUNAR_TEAMS_SERVICE_*`; supervisord passes its environment to the
program, so `docker run -e`, compose `environment:` or `ENV` are all that is needed.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_TEAMS_SERVICE_USER` | `MALRUNAR_MSTEAMS_MCP_USER`, else `MALRUNAR_WORK_BROWSER_USER` | The user the program runs as when it starts as root. Missing then: exit 78. `MALRUNAR_TEAMS_SERVICE_ALLOW_ROOT=1` keeps root |
| `MALRUNAR_TEAMS_SERVICE_ENABLED` | `1` | `0`: exit 0 without starting |
| `MALRUNAR_TEAMS_SERVICE_DATA_DIR` | `$HOME/.local/share/malrunar-teams-service` | PocketBase data (`pb_data/`, with its nightly backup zips), `pb-credentials.json`, `service-token`, cached media. Mount a volume here |
| `MALRUNAR_TEAMS_SERVICE_PORT` | `18390` | The HTTP API |
| `MALRUNAR_TEAMS_SERVICE_BIND` | `127.0.0.1` | The API's address. The API has a bearer token but no TLS: put a proxy in front of it rather than binding wider |
| `MALRUNAR_TEAMS_SERVICE_PB_PORT` | `18391` | PocketBase (always `127.0.0.1`; only the service, as superuser, talks to it) |
| `MALRUNAR_TEAMS_SERVICE_PB_URL` | `http://127.0.0.1:<PB_PORT>` | The PocketBase the service uses (for a PocketBase you run yourself) |
| `MALRUNAR_TEAMS_SERVICE_TOKEN` | none | The bearer token of the API (32 characters or more). Removed from the environment at start |
| `MALRUNAR_TEAMS_SERVICE_TOKEN_FILE` | `<data dir>/service-token` | A file holding the token. When neither this nor the variable is set the service creates `service-token` (mode 0600, random) on first start: `cat` it to get the token |
| `MALRUNAR_TEAMS_SERVICE_TIME_ZONE` | the process time zone (`TZ`), else `UTC` | Zone for all-day events created through the API and for events without a zone (a Windows or IANA name) |
| `MALRUNAR_TEAMS_SERVICE_REQUEST_GAP_MS` | `250` | Minimum gap between requests to Microsoft (one request at a time; interactive work goes first) |
| `MALRUNAR_TEAMS_SERVICE_BACKUP_CRON` | `20 3 * * *` | PocketBase's backup schedule (the last two zips are kept); `off` leaves PocketBase's setting alone |
| `MALRUNAR_TEAMS_SERVICE_LOG_LEVEL` | `info` | `error`, `warn`, `info`, `debug` (JSON lines on stdout) |
| `MALRUNAR_TEAMS_SERVICE_USER_HEADER`, `GROUPS_HEADER`, `NAME_HEADER`, `EMAIL_HEADER` | `Remote-User`, `Remote-Groups`, `Remote-Name`, `Remote-Email` | Names of the headers an authenticating proxy sets ("Behind an authenticating proxy") |
| `MALRUNAR_TEAMS_SERVICE_TRUSTED_PROXIES` | none | Comma-separated addresses or CIDRs (IPv4 and IPv6) the proxy connects from |
| `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_HEADER`, `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_FILE` | none | A header the proxy sets to a shared secret, and the file holding the secret (one per line, two at most). Set both or neither |
| `MALRUNAR_TEAMS_SERVICE_NODE` | see Dependencies | The Node to run |
| `MALRUNAR_TEAMS_SERVICE_MCP_DIST` | `/opt/malrunar/msteams-mcp/app/dist` | The `msteams-mcp` package's `dist/` |
| `MALRUNAR_TEAMS_SERVICE_MCP_STATE_DIR` | `$HOME/.teams-mcp-server` | The directory the guard protects: where the package keeps its session (it is not configurable in the package, so leave it) |

## Behind an authenticating proxy

Authentication is external: put a reverse proxy that signs users in (Authelia, for example) in front of the
service. The service reads who the user is from headers the proxy sets (names configurable, defaults are
Authelia's): `Remote-User`, `Remote-Groups` (comma-separated), `Remote-Name`, `Remote-Email`. The bearer
token stays required; the identity is additional.

Today the identity is used for one thing: `GET /whoami` returns it (`user`, `name`, `email`, `groups`,
`trusted`, `anonymous`) and the debug log names the user on each request line. The service holds one
Teams account and has no per-user state, so there are no allow-lists and no request is rejected because of
who made it. Richer use of the groups can be added later.

Headers sent by anyone can say anything, so they count only from a trusted source:

- `MALRUNAR_TEAMS_SERVICE_TRUSTED_PROXIES`: the connecting peer address (never `X-Forwarded-For`) must be in the list.
- `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_HEADER` with `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_FILE`: the proxy must send the secret in that header. The file
  has one secret per line (at least 16 characters; `#` comments and blank lines ignored). Two secrets are
  accepted at once for a rotation: add the new one, change the proxy, then remove the old one. The file is
  read again when it changes.
- Both set: both must pass. Neither set: nothing is trusted, every request is anonymous and the service logs a
  warning at start. A request that fails the check is not rejected, only anonymous.

The secret and identity headers are never logged. A bad setting makes the service exit with status 78.

Authelia with Traefik, with generic names: the `forwardAuth` middleware copies the identity headers from
Authelia's reply, then a second `headers` middleware sets the secret after it (so a client cannot supply
it and the identity headers are only ever Authelia's):

```yaml
http:
  middlewares:
    teams-authelia:
      forwardAuth:
        address: http://authelia:9091/api/authz/forward-auth
        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Name, Remote-Email]
    teams-proxy-secret:
      headers:
        customRequestHeaders:
          X-Proxy-Secret: "<the secret in the file>"
  routers:
    teams:
      rule: Host(`teams.example.org`)
      middlewares: [teams-authelia, teams-proxy-secret]   # order matters
      service: teams-service
```

with `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_HEADER=X-Proxy-Secret`, `MALRUNAR_TEAMS_SERVICE_PROXY_SECRET_FILE=/run/secrets/teams-proxy-secret` and, if
the proxy has a fixed address, `MALRUNAR_TEAMS_SERVICE_TRUSTED_PROXIES=<its address or network>`.

## The HTTP API

Every route but `GET /health` needs `Authorization: Bearer <token>`. JSON in and out.

| Route | What it does |
|---|---|
| `GET /health` | `{"ok": true}` |
| `GET /whoami` | The proxy-supplied identity (see above); anonymous when nothing is trusted |
| `GET /status` | Sign-in state (`auth.state`: `ok`, `signed-out`, ...), sync progress and counts |
| `GET /chats`, `GET /chats/<id>`, `GET /chats/<id>/messages` | The local copy of chats and their messages (rendered to safe HTML, media and files resolved to cached copies) |
| `GET /search` | Full-text search of the copy |
| `GET /media/<key>` | A cached image or file |
| `POST /sync` | Poll now |
| `POST /chats/<id>/send`, `/upload`, `/read`, `/messages/<id>/edit`, `/delete`, `/reaction` | Live writes (503 with `signedOut: true` when the session is gone) |
| `GET /calendar/status`, `/calendar/events`, `/calendar/events/<id>` | The calendar copy (default and subscribed calendars) |
| `POST /calendar/sync`, `/calendar/events`, `/calendar/events/<id>/update`, `/respond`, `/delete` | Calendar writes, through Microsoft Graph with the session's own client; subscribed calendars are read-only |

## What is stored

PocketBase collections (superuser-only, no API rules), created by the Go migrations compiled into
`malrunar-teams-pocketbase`: `chats`, `messages`, `people`, `media`, `state`, `calendar_events`, and
`push_subscriptions` and `push_state`. **Web push.** The last two hold browser push subscriptions and detector
state for a web app that sends notifications about new messages; this service creates the schema and
keeps it in its database but sends no push messages itself (the sender belongs to your web app, which
reads those collections through the service's PocketBase). The migrations are named as they were when the
schema was created (`1791880000_teams_copy.go`, ...): an existing PocketBase data directory from the same
schema is compatible.

PocketBase is built from source in the image build: version `0.38.2` is pinned in
[`pins.env`](pins.env) and in `pocketbase/go.mod` (the build asserts that they agree), every Go module it needs is pinned
by sha256 in `pocketbase/go.sum` and verified by `go build -mod=readonly`, and the Go toolchain image by digest.

## Synthetic fixtures

[`dev/fixture.mjs`](dev/fixture.mjs) starts the real server, sync and store code over an in-memory store and
a scripted Teams world of made-up people and chats (`node dev/fixture.mjs`: API on `127.0.0.1:28390`, control
port on `28399` to sign out, sign in or inject a message), for working on a web view without Microsoft.
Every name, chat id, address and identifier in the fixtures and tests is synthetic (`example.invalid`,
all-same-digit GUIDs); the only real identifiers are public Microsoft application ids and domains that the
code needs to talk to Microsoft.

## Tests

`test/` holds the unit tests (`node --test`): the sync, the PocketBase store (against the real binary
the image ships), the HTTP server, the forward-auth identity (with the shared vectors), the message renderer, the media cache, the calendar and the session
guard (against the real `msteams-mcp` package). The smoke test runs them inside the image with the `node`
component. To run them from a checkout of this repository:
`MALRUNAR_TEAMS_SERVICE_MCP_DIST=<a built msteams-mcp dist> MALRUNAR_TEAMS_SERVICE_TEST_PB_BIN=<binary> node --test teams-service/test/*.test.mjs`.

## Licences

The recipe and the scripts of this directory are Apache-2.0 ([LICENSE](LICENSE)). The image contains
PocketBase (MIT) and the Go modules it is built from, listed in `THIRD-PARTY.txt` with their licence files in
`licenses/`, and `SOURCE.txt` records how the binary was built. The Node service has no third-party
packages. Microsoft Teams and Graph are Microsoft's; see the `msteams-mcp` README for what using them
this way means.
