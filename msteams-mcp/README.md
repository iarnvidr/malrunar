# msteams-mcp

The Microsoft Teams MCP server [m0nkmaster/msteams-mcp](https://github.com/m0nkmaster/msteams-mcp) (MIT),
built at a pinned commit with a handful of build-time patches, plus the plumbing that lets it sign in on
a remote desktop and share that sign-in with the browser you look at. As a component image it holds the
package, a pinned Playwright Firefox, and one command, `malrunar-msteams-mcp`.

How the upstream server works: a person signs in to Microsoft once in a browser it drives with
Playwright; the server then reads the Teams web app's own MSAL tokens out of that browser profile and
makes its routine calls over plain HTTPS to Teams' internal endpoints. No Azure app registration and no
Graph permissions are involved. It speaks MCP on stdin/stdout only (the build fails if an HTTP transport
ever appears in the package).

**This talks to undocumented Microsoft endpoints with tokens taken from your own sign-in.** Use it with
an account and a tenant where that is allowed, and read [Device-code sign-in](#device-code-sign-in-optional-off-by-default)
before you turn on the one module that goes further.

## Dependencies

Every component is opt-in; this one declares what it needs and checks it. Each command of
`malrunar-msteams-mcp` starts by checking all of the first three and exits with status 78 (configuration
error) and a message that names what is missing. The smoke test runs it once with each of them absent.

| Needs | Why |
|---|---|
| [`node`](../node/) component | Node 24 runs the package, and `node:sqlite` writes the cookies of `sync-browser`. Found at `/opt/malrunar/node/bin/node`, else on `PATH`, else `MALRUNAR_MSTEAMS_MCP_NODE` |
| [`desktop-base`](../desktop-base/) | The libraries the Playwright Firefox loads (GTK, NSS, ...), the stock Firefox, `setpriv`, `supervisorctl`, `dbus`, the X utilities |
| [`work-browser`](../work-browser/) component | The visible, persistent browser: `open` hands pages to it, `sync-browser` writes the sign-in into its profile, `browser-mcp` looks at its windows. Its launcher and display search are reused, not copied |
| a desktop user | Created in your own layer. The session store is encrypted with a key derived from the **host name and the user name**: every command of this component, and the `teams-service` component, must run as the same user (`MALRUNAR_MSTEAMS_MCP_USER`, else `MALRUNAR_WORK_BROWSER_USER`) |

Optional, only for what is named:

| For | Needs |
|---|---|
| `browser-mcp` (the visual browser MCP) | `xdotool`, `imagemagick`, `xclip` and `poppler-utils` from your own layer (`x11-utils` and `python3` come with `desktop-base`). It exits with status 78 and the list of what is missing |
| `browser-mcp` with the Tail backend | the [`tail-browser-mcp`](../tail-browser-mcp/) component (`MALRUNAR_MSTEAMS_MCP_BROWSER_MCP_BACKEND=tail`); it exits with status 78 naming it when it is absent |
| a local copy of your chats and calendar | the [`teams-service`](../teams-service/) component, which needs `node` and this component's package |

It does **not** need `obs-*`, `media-bridge`, `session-kit`, a Guacamole gateway or any other component.
The component has **no supervisord program**: the MCP server is started per session by an MCP client,
and the long-lived browser is the `work-browser` component's program.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/node:24.22.1 / /
COPY --link --from=ghcr.io/iarnvidr/work-browser:0.1.0 / /
COPY --link --from=ghcr.io/iarnvidr/msteams-mcp:0.27.2 / /
RUN useradd --create-home --uid 1000 --shell /bin/bash me
ENV PATH=/opt/malrunar/node/bin:$PATH MALRUNAR_WORK_BROWSER_USER=me
```

[`share/Dockerfile.example`](share/Dockerfile.example) is a longer example. Keep `/home/me/.teams-mcp-server`
(the session store) on a volume, and give the container a fixed host name.

```sh
docker exec -it desktop /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp login --force   # sign in once, in the desktop session
docker exec -i  desktop /opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp                  # what your MCP client starts
```

| Path | What it is |
|---|---|
| `/opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp` | The command |
| `/opt/malrunar/msteams-mcp/app/` | The package (`dist/`, `node_modules/`, `package-lock.json`), patched |
| `/opt/malrunar/msteams-mcp/browsers/firefox-<revision>/` | Playwright's Firefox build |
| `/opt/malrunar/msteams-mcp/libexec/` | `browser-login.mjs`, `sync-browser-session.mjs`, `cookie-mirror.mjs`, `device-login.mjs`, `work-browser-mcp.mjs` |
| `/opt/malrunar/msteams-mcp/lib/common.sh`, `share/`, `licenses/`, `SOURCE.txt`, `THIRD-PARTY.txt` | Shell helpers, examples, licences and provenance |

Nothing is added to `PATH` and nothing is created outside `/opt/malrunar/msteams-mcp`.

## Commands

| Command | What it does |
|---|---|
| `malrunar-msteams-mcp` (or `serve`) | The MCP server on stdin/stdout, with `LOG_LEVEL=warn` (see [LOG_LEVEL](#log_level)). Needs no display |
| `login [--force]` | `teams_login`: an interactive sign-in in the package's own Firefox, on the desktop display. `--force` clears the cookies first, which is needed whenever an earlier attempt left a half-signed-in profile. Exits with status 3 when no display answers |
| `status` | `teams_status`: session age, token state |
| `login-browser` | A sign-in with no person at the screen: types the account (`MALRUNAR_MSTEAMS_MCP_UPN`, no default) and the password (**stdin only**), waits for you to approve multi-factor authentication on your phone, then mirrors the cookies into the visible browser. See [Signing in](#signing-in) |
| `sync-browser [--dry-run]` | Copies the Microsoft web cookies of the MCP session into the visible browser's profile so one sign-in covers both |
| `device-login [--region-only]` | **Off unless `MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1`.** See [Device-code sign-in](#device-code-sign-in-optional-off-by-default) |
| `open [url]` | `malrunar-work-browser-open`: a tab in the running visible browser |
| `browser-mcp` | A second MCP server (stdio) that lists, reads, screenshots, clicks in and navigates the visible browser's windows. See [browser-mcp](#browser-mcp) |
| `check` | Verifies the dependencies and the libraries of the Playwright Firefox; names anything missing |

When started as root (a `docker exec` or supervisord) the command switches to the desktop user with
`setpriv` before it does anything else.

## Configuration

All variables are optional except where the table says otherwise. The patched package reads some
`MSTEAMS_*` variables itself; the command sets them from the `MALRUNAR_MSTEAMS_MCP_*` ones, so use these.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_MSTEAMS_MCP_USER` | `MALRUNAR_WORK_BROWSER_USER` | The user that owns the Teams session. Required when started as root, else exit 78. `MALRUNAR_MSTEAMS_MCP_ALLOW_ROOT=1` keeps root on purpose |
| `MALRUNAR_MSTEAMS_MCP_NODE` | `/opt/malrunar/node/bin/node`, else `node` on `PATH` | The Node to run (22 or newer) |
| `MALRUNAR_MSTEAMS_MCP_LOG_LEVEL` | `warn` | `warn` or `error`; anything else is turned into `warn` |
| `MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR` | `MALRUNAR_WORK_BROWSER_PROFILE_DIR`, else `$HOME/.config/malrunar-work-browser/profile` | The profile of the **visible** browser: where `sync-browser` writes cookies and whose windows `browser-mcp` exposes |
| `MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL` | `auto` | How the visible browser is stopped while its profile is written: `supervisor` (`supervisorctl stop` / `start` of its program; needs access to the supervisord socket, usually root), `kill` (ends the Firefox on that profile and lets its launcher restart it), `none` (you stopped it), `auto` (`supervisor` when `supervisorctl status` answers, else `kill`) |
| `MALRUNAR_MSTEAMS_MCP_BROWSER_PROGRAM` | `work-browser` | The supervisord program of the visible browser |
| `MALRUNAR_MSTEAMS_MCP_SUPERVISOR_CONF` | `/etc/supervisor/supervisord.conf` | The config `supervisorctl` is given |
| `MALRUNAR_MSTEAMS_MCP_FIREFOX` | the Playwright Firefox of the component | The Firefox the package drives. Only Playwright's build works; a stock Mozilla Firefox hangs (see the Firefox patch) |
| `MALRUNAR_MSTEAMS_MCP_LOGIN_TIMEOUT_MS` | `600000` | One hard wall for the whole `teams_login` call, which has to cover credentials, MFA and the "stay signed in" prompt |
| `MALRUNAR_MSTEAMS_MCP_TEAMS_URL` | `https://teams.cloud.microsoft` | Where `login` goes (set `https://teams.microsoft.com` for the legacy domain) |
| `MALRUNAR_MSTEAMS_MCP_TOKEN_WAIT_MS` | `90000` | How long `login` waits for a Substrate search token |
| `MALRUNAR_MSTEAMS_MCP_UPN` | none | `login-browser`: the account (a user principal name). Required there |
| `MALRUNAR_MSTEAMS_MCP_MFA_TIMEOUT_MS` | `240000` | `login-browser`: how long to wait for MFA approval |
| `MALRUNAR_MSTEAMS_MCP_LOGIN_URL` | `https://myaccount.microsoft.com/` | `login-browser`: the page that forces a real sign-in |
| `MALRUNAR_MSTEAMS_MCP_FORCE_BROWSER_LOGIN` | `0` | `login-browser`: `1` clears the Microsoft login cookies first |
| `MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN` | `0` | `1` turns `device-login` on |
| `MALRUNAR_MSTEAMS_MCP_TENANT` | `organizations` | `device-login`: the tenant |
| `MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID` | the Teams desktop client | `device-login`: the public client to sign in as |
| `MALRUNAR_MSTEAMS_MCP_SYNC_AFTER_DEVICE_LOGIN` | `0` | `1` runs `sync-browser` after a successful `device-login` |
| `MALRUNAR_MSTEAMS_MCP_BROWSER_MCP_BACKEND` | `bidi` | `browser-mcp`: how page text is read: `bidi` (WebDriver BiDi) or `tail` |
| `MALRUNAR_MSTEAMS_MCP_BIDI_URL` | `ws://127.0.0.1:9222/session` | `browser-mcp`: where Firefox's BiDi server listens |
| `MALRUNAR_MSTEAMS_MCP_TAIL_CLIENT` | `/opt/malrunar/tail-browser-mcp/client.mjs` | `browser-mcp` with `tail`: the Tail client |
| `MALRUNAR_MSTEAMS_MCP_DOWNLOADS_DIR` | `$HOME/Downloads` | `browser-mcp`: where `browser_file_text` looks for the downloaded file |

The session lives where upstream puts it, `$HOME/.teams-mcp-server` (`session-state.json`,
`token-cache.json`, the package's browser profile in `browser-profile/`). That path is not configurable:
mount a volume at it.

### The token cache key

The package encrypts its session files with a key derived from `os.hostname()` and the user name
(`os.userInfo().username`). A different host name or user name cannot read the files and the symptom is a
request to sign in again. So run the container with a fixed host name and keep the user (and its uid)
stable. The build asserts that upstream still derives the key this way.

### LOG_LEVEL

Upstream logs at `info` by default and writes `info` and `debug` to **stdout**, which is the MCP
transport: one log line corrupts the protocol. The command therefore always runs the server with
`LOG_LEVEL=warn` (or `error`). The build asserts that upstream still logs to stdout that way, and the
smoke test checks that the server's stdout is clean JSON-RPC even when `info` is requested.

## Signing in

There are three ways to get a session; use the one your tenant allows.

1. **`login` (interactive)**: the package's own Firefox opens on the desktop, you sign in, it saves the
   session. This is upstream's flow, with two fixes (see [Patches](#patches)). Run it as often as the
   tenant's policy requires.
2. **`login-browser` (no person at the screen)**: types the account and the password (read from stdin,
   so it never reaches `ps`, `docker inspect` or the filesystem) into the package's browser, shows the
   number-match code of multi-factor authentication if there is one and waits for you to approve it
   on your phone, clears the "stay signed in" prompt, then mirrors the Microsoft cookies into the visible
   browser. `printf '%s\n' "$PASSWORD" | docker exec -i -e MALRUNAR_MSTEAMS_MCP_UPN=user@example.invalid desktop .../malrunar-msteams-mcp login-browser`.
   It does not write the MCP's own `session-state.json`: it signs the **browser** in.
3. **`device-code`**: optional and off by default, below.

**Two browsers, two profiles.** The package drives its own Firefox (Playwright's build, juggler protocol,
profile `~/.teams-mcp-server/browser-profile`). The visible browser is the `work-browser` component's stock
Firefox with its own profile. They are separate on purpose: Firefox refuses a profile last written by a
newer version, and the two builds differ. `sync-browser` and `login-browser` bridge them by copying the
Microsoft cookies (the broad single-sign-on cookie `ESTSAUTHPERSISTENT` among them) into the visible
profile's `cookies.sqlite`, one way, while the visible browser is stopped
(`MALRUNAR_MSTEAMS_MCP_BROWSER_CONTROL`). Whether Microsoft web apps then accept that cookie without asking
again is up to your tenant's Conditional Access policy. If you really want one shared profile, set
`MALRUNAR_WORK_BROWSER_PROFILE_DIR` and `MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR` to
`$HOME/.teams-mcp-server/browser-profile`: the commands then stop the visible browser while they run and clear
the profile's version stamp, but a profile written by a newer Firefox than Playwright's can still misbehave.

## browser-mcp

`browser-mcp` is a stdio MCP server for an AI agent that should see what you see: `browser_windows`,
`browser_tabs`, `browser_screenshot`, `browser_read` (text of the page, read without changing the
selection or focus), `browser_file_text` (downloads a document through the signed-in browser and extracts
its text), `browser_click`, `browser_type`, `browser_key`, `browser_scroll`, `browser_navigate`. It only
exposes windows whose Firefox was started with the visible browser's profile. It drives the display with
`xdotool`, so **it can type and click in the desktop session**: give it to agents you would give the keyboard to.

Reading page text needs the browser started with a BiDi port; with the work-browser component:
`MALRUNAR_WORK_BROWSER_ARGS=--remote-debugging-port=9222`. (Port 9222 is Firefox's remote agent on the
loopback interface of the container; do not publish it.) With the Tail backend the page is read through that
extension instead. The smoke test runs it against the real work browser under a virtual display.

## Patches

The package is built from the pinned upstream commit, then [`patches/apply.sh`](patches/apply.sh) applies
five patches to the compiled `dist/`. **The build fails** when a patch does not apply to the pinned commit,
when a patch changes nothing, and when an assumption this component relies on is no longer true (the
logging stream, the state directory name, the key derivation, stdio-only transport, the exports the
modules of `libexec/` import). The patches are generic fixes, offered upstream in
[docs/upstream-msteams-mcp.md](../docs/upstream-msteams-mcp.md) (ready-to-file descriptions, not filed).

| Patch | Fixes |
|---|---|
| [`browser-firefox.cjs`](patches/browser-firefox.cjs) | Launches Firefox instead of the hard-coded Chromium channel; `PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH` selects the binary. Playwright only drives its own Firefox build, so the component ships that |
| [`login-timeout.cjs`](patches/login-timeout.cjs) | `teams_login --force` timing out before a person sees the page (a doomed headless attempt ate the shared timeout); the timeout becomes `MSTEAMS_LOGIN_TIMEOUT_MS`, default 10 minutes |
| [`substrate-token.cjs`](patches/substrate-token.cjs) | A "successful" login that left no Substrate token: login on `teams.cloud.microsoft`, any Teams domain accepted, the search box primed so Teams acquires the token, messaging cookies read from both domains (newest wins) |
| [`refresh-origin.cjs`](patches/refresh-origin.cjs) | The refresh sent the browser `Origin` header for every client; Entra rejects it for non-SPA clients, so a session made by another public client could never be renewed |
| [`refresh-expired.cjs`](patches/refresh-expired.cjs) | After an idle hour every access token has expired, and the renewal gave up before trying the HTTP refresh |

The unit tests run the refresh patches against the shipped package with a scripted Microsoft
(`test/unit/`), and the smoke test starts the Playwright Firefox through the patched package.

**Upstream has moved on.** The pinned commit is the one npm published as 0.27.2; upstream is at 0.30.x, where
some of these are fixed and the dist layout differs, so four of the five patches do not apply there. Moving
the pin means re-reading each patch against the new source (the build tells you which no longer apply).

## Device-code sign-in (optional, off by default)

`device-login` prints a code to approve at the address Microsoft names, from any phone, and seeds the
session without a browser. To be able to redeem a device code without a client secret it signs in as the
**Microsoft Teams desktop application**, Microsoft's first-party public client (its application id is public and
is in the source). That is the caveat:

- it is **not an application you registered**; using it from a script is not something Microsoft documents,
  and it may breach terms that apply to you;
- your tenant may block device-code sign-in, flag or alert on sign-ins by that client (Conditional Access,
  sign-in logs, security tooling), or revoke the session;
- the refresh tokens of that client are not capped at 24 hours like those of the web client, which is why it
  is useful, and also why it deserves your tenant administrator's agreement;
- a session made this way carries no browser state: the visible browser is signed in only if you also
  run `sync-browser` and the cookie jar already held the Microsoft cookies.

So it runs only when `MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1`, and `libexec/device-login.mjs` refuses to run
without it even when called directly (exit status 78, no request is made; a test checks both). The generic
patch that lets the package renew such a session (`refresh-origin`) is applied regardless: it only stops sending a
header that other clients cannot use. Use `MALRUNAR_MSTEAMS_MCP_TENANT` and `MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID` to
sign in to a specific tenant or as another client of your own.

## Licences

The recipe and the scripts of this directory are Apache-2.0 ([LICENSE](LICENSE)). The image contains:

- msteams-mcp, MIT, copyright Rob MacDonald (`licenses/msteams-mcp-LICENSE`), at the commit named in
  `SOURCE.txt`, with the patches of `patches/` applied (they are part of this repository's Apache-2.0
  recipe and are offered upstream);
- its production npm dependencies, each under its own licence, listed in `THIRD-PARTY.txt` and kept in
  `app/node_modules/<name>/` (MIT, Apache-2.0 for Playwright, `licenses/playwright-LICENSE`, and others);
- Playwright's Firefox build, MPL-2.0 (`licenses/MPL-2.0`): built and distributed by the Playwright project
  from the Mozilla sources with Playwright's own patches, downloaded at build time from `cdn.playwright.dev`,
  verified by sha256, unmodified by this recipe. **It is not Mozilla's Firefox** and carries no Mozilla
  endorsement; it is the only browser this protocol works with. Source locations are in `SOURCE.txt`.
  The stock Firefox of `desktop-base` stays unmodified (nothing here touches it).

## Not included

Policies for the visible browser (camera and microphone permissions for Teams, see
[`share/policies.teams.json.example`](share/policies.teams.json.example)), the user, the host name, secrets
handling for a password and any MCP client wiring belong in your own image and deployment.
