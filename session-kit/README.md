# session-kit

Small, independent tools for the XFCE desktop session of a remote desktop, shipped as one command,
`malrunar-session`:

| Subcommand | What it does | Extra needs |
|---|---|---|
| `workspaces` | Names the XFCE workspaces and keeps moving matching windows (by window class and role) to the workspace you choose, once per window. Optional panel tweaks. | `xdotool` |
| `restore-tabs [profile]` | Prints the http(s) tab URLs of a Firefox profile's last session, NUL separated. | none |
| `open-tabs [profile]` | Opens those tabs in the running work browser. | `work-browser` component |
| `check` | Checks the session: display, workspaces, window counts, optionally the work browser and its add-ons. Exit status 1 and one line per problem. | `xdotool`; `work-browser` for its checks |
| `launchers` | Registers a command as the desktop's web browser (menu entry, XFCE "Web Browser" helper, links from other apps) for the user. | `work-browser` component, or `MALRUNAR_SESSION_KIT_BROWSER_COMMAND` |
| `clear-super-shortcuts` | Removes Xfce's `<Super>` application shortcuts from the user's keyboard settings (a Mac client sends Cmd as Super). Window-manager bindings stay. | none |

Nothing here knows a user, path, site or product: every setting is a `MALRUNAR_SESSION_KIT_*`
environment variable (below), and everything is opt-in. The component is `FROM scratch`: scripts, a
README and one supervisord fragment.

## Dependencies

| Needs | Why |
|---|---|
| [`desktop-base`](../desktop-base/) | python3, the X11 utilities (`xdpyinfo`, `xprop`), XFCE with `xfconf-query`, supervisor, `liblz4.so.1` |
| a desktop user | created in your own layer; named in `MALRUNAR_SESSION_KIT_USER` when a program starts as root |
| `xdotool` | `workspaces` and `check` find and move windows with it. `desktop-base` does not ship it: install the package in your own layer (see [Compose](#compose)). Without it these subcommands exit 78 and say so |
| a desktop session | an X display with the XFCE window manager and settings daemon (your xrdp session); `workspaces` waits for it |

Optional:

| Optional | Used by | Without it |
|---|---|---|
| [`work-browser`](../work-browser/) | `open-tabs`, `launchers` (default command), `check` with `MALRUNAR_SESSION_KIT_CHECK_WORK_BROWSER=1`, and the default profile of `restore-tabs` | each of these exits 78 with a message naming the component; `workspaces`, `restore-tabs <dir>`, `launchers` with a command of your own, `clear-super-shortcuts` and the other checks work |

It does **not** need `node`, a browser of its own or any other component. The smoke test enforces
this: without `desktop-base` the command exits 78 naming it, the test image has no `node`, no
`xdotool` and no `work-browser` for the "absent" half (each subcommand must then fail with a message
naming what it needs), then the same tools are run for real with `xdotool` and a virtual X server,
and with `work-browser` present; and the component's own files are only the ones listed below.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/session-kit:0.1.0 / /
COPY --link --from=ghcr.io/iarnvidr/work-browser:0.1.0 / /     # optional: only for the work-browser tools
RUN apt-get update && apt-get install -y --no-install-recommends xdotool && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 --shell /bin/bash me
ENV MALRUNAR_SESSION_KIT_USER=me MALRUNAR_WORK_BROWSER_USER=me \
    MALRUNAR_SESSION_KIT_WORKSPACES="Work,Chat,Scratch" \
    MALRUNAR_SESSION_KIT_PLACE="classname=Navigator,role=browser@Work"
```

Copying the component in adds the supervisord program `session-kit`
(`/etc/supervisor/conf.d/malrunar-session-kit.conf`, the
[supervisord convention](../CONVENTIONS.md#supervisord-programs-of-components)). It runs
`malrunar-session workspaces`, which with none of `MALRUNAR_SESSION_KIT_WORKSPACES`,
`MALRUNAR_SESSION_KIT_PLACE` and `MALRUNAR_SESSION_KIT_PANEL_TWEAKS` set does nothing and stays idle, so
the component can be present without acting. An image that does not want the program deletes the file in
its own layer. The other subcommands are run by you: from a session autostart entry, a container
health check, an entrypoint or by hand, for example
`docker exec -u me desktop /opt/malrunar/session-kit/bin/malrunar-session check`.

Examples of the pieces:

```sh
# put the work browser's window on workspace "Work" and mail-like windows on "Chat"
MALRUNAR_SESSION_KIT_WORKSPACES="Work,Chat,Scratch"
MALRUNAR_SESSION_KIT_PLACE="classname=Navigator,role=browser@Work;class=Mail@Chat"

# reopen the pages of the last session in the work browser, after a profile was reset
malrunar-session open-tabs

# make the work browser what links from other apps open
malrunar-session launchers

# a health check: one browser window, owned by the work browser, on its profile; two workspaces
MALRUNAR_SESSION_KIT_CHECK_WORK_BROWSER=1 MALRUNAR_SESSION_KIT_WORKSPACES="Work,Chat" malrunar-session check
```

## Settings

All optional. A program or command that starts as root needs `MALRUNAR_SESSION_KIT_USER`; one that
starts as a normal user keeps that user.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_SESSION_KIT_USER` | none | The desktop user. A subcommand started as root switches to it (and fails with status 78 when it is unset or unknown) |
| `MALRUNAR_SESSION_KIT_DISPLAY` | `$DISPLAY` if it answers, else the newest X socket that answers | X display to use |
| `MALRUNAR_SESSION_KIT_DBUS_ADDRESS` | `$DBUS_SESSION_BUS_ADDRESS`, else `$XDG_RUNTIME_DIR/bus` | D-Bus session bus of the XFCE session (for `xfconf-query`) |
| `MALRUNAR_SESSION_KIT_WORKSPACES` | none | Comma separated workspace names; their number sets the workspace count. Unset: the count is left alone |
| `MALRUNAR_SESSION_KIT_PLACE` | none | `;` separated rules `<selector>@<workspace>`. A selector is `class=<WM_CLASS class>` or `classname=<WM_CLASS instance>`, optionally `,role=<WM_WINDOW_ROLE>`; the workspace is a 1-based number or one of the names above. Values may contain letters, digits, space, `_`, `.`, `-`. A window is moved once, so the user can move it again |
| `MALRUNAR_SESSION_KIT_PANEL_TWEAKS` | `0` | `1`: the task list shows only the current workspace's windows, ungrouped, and the pager is one row of names |
| `MALRUNAR_SESSION_KIT_POLL_SECONDS` | `2` | Seconds between looks for new windows |
| `MALRUNAR_SESSION_KIT_WAIT_SECONDS` | `0` (forever; `120` with `--once`) | How long `workspaces` waits for the session before failing |
| `MALRUNAR_SESSION_KIT_PROFILE_DIR` | the work browser's profile, only when the component is present | The Firefox profile whose last session `restore-tabs`/`open-tabs` read (also the add-on list for `check`) |
| `MALRUNAR_SESSION_KIT_FALLBACK_URL` | none | URL to use when the session holds no tab. Unset: no tabs |
| `MALRUNAR_SESSION_KIT_MAX_TABS` | `200` | At most this many tabs are restored |
| `MALRUNAR_SESSION_KIT_OPEN_TIMEOUT` | `30` | Seconds each handoff of `open-tabs` may take |
| `MALRUNAR_SESSION_KIT_WORK_BROWSER_DIR` | `/opt/malrunar/work-browser` | Where the work-browser component is installed. The kit reads that component's own `MALRUNAR_WORK_BROWSER_PROFILE_DIR` for the profile |
| `MALRUNAR_SESSION_KIT_BROWSER_COMMAND` | the work browser's open helper | The command `launchers` registers; it is called with the URL as its argument |
| `MALRUNAR_SESSION_KIT_LAUNCHER_NAME` | `Web Browser` | Name of the menu entry |
| `MALRUNAR_SESSION_KIT_LAUNCHER_ID` | `malrunar-session-kit-browser` | File name of the `.desktop` entries and the XFCE helper id |
| `MALRUNAR_SESSION_KIT_LAUNCHER_ICON` | `internet-web-browser` | Icon theme name or absolute icon path written to both launcher entries |
| `MALRUNAR_SESSION_KIT_CHECK_WINDOWS` | none | `check`: `;` separated `<selector>,count=<n>` (default count 1): exactly that many windows must match |
| `MALRUNAR_SESSION_KIT_CHECK_WORK_BROWSER` | `0` | `check`: `1` verifies the work browser: it holds the profile lock, was started with `-profile <that profile>`, runs without a WebDriver or debugging port, and exactly one browser window exists and is its own. Needs the component |
| `MALRUNAR_SESSION_KIT_CHECK_BROWSER_WINDOWS` | `1` | `check`: the expected number of browser windows for the check above, or `any` |
| `MALRUNAR_SESSION_KIT_CHECK_ALLOW_WEBDRIVER` | `0` | `check`: `1` allows `--marionette`/`--remote-debugging-port` |
| `MALRUNAR_SESSION_KIT_CHECK_ADDONS` | none | `check`: comma separated add-on ids that must be active in the profile |

`workspaces --once` applies everything once and exits (the supervisord program keeps running).
Exit status 78 means the image or a setting cannot do what was asked, with a message that names it.

`launchers` writes both desktop entries with `MALRUNAR_SESSION_KIT_LAUNCHER_ICON` (default `internet-web-browser`), which can be a theme icon name or absolute path. It writes only inside the user's home (`~/.local/share/applications`,
`~/.local/share/xfce4/helpers`, and the `WebBrowser=` line of `~/.config/xfce4/helpers.rc`).

## Files

The component adds nothing outside `/opt/malrunar/session-kit` except
`/etc/supervisor/conf.d/malrunar-session-kit.conf`:

- `bin/malrunar-session`, `lib/session_kit.py` (standard library only), `lib/common.sh`
- `share/Dockerfile.example`
- `README.md`, `LICENSE`

## Tests

`test/test_session_kit.py` runs on any host with python3 (the X tools are replaced by `test/fake_x.py`;
CI runs it in the `scripts` job and again inside a `desktop-base` image). `test/smoke.sh` is the
bounded image test described above, including a virtual X server with a real window manager.
