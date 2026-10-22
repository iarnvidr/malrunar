# work-browser

A persistent Firefox for the desktop's user: the launcher keeps it running on a profile that survives
restarts, and a helper opens URLs in it. It is meant for a "work desktop": the browser you sign in
to your accounts with and leave open, as opposed to a call app or a throwaway automation browser.

The component is `FROM scratch` and holds scripts and a supervisord program only. **It ships no
Firefox.** It runs the unmodified official Firefox ESR that [`desktop-base`](../desktop-base/)
installs in `/opt/malrunar/firefox`, so nothing in this component changes the Firefox build
(see [Firefox stays unmodified](#firefox-stays-unmodified)).

## Dependencies

| Needs | Why |
|---|---|
| [`desktop-base`](../desktop-base/) | Firefox in `/opt/malrunar/firefox`, `dbus-daemon`, `xdpyinfo`, `setpriv`, `pgrep`, supervisord |
| a desktop user | created in your own layer; named in `MALRUNAR_WORK_BROWSER_USER` |
| a desktop session | an X display to show windows on (your xrdp session); the launcher waits for it |

It does **not** need `node` (add the [`node`](../node/) component only if something else in your image
does) and no other component. The smoke test enforces this: without `desktop-base` the launcher exits
with status 78 and a message naming it, the test image has no `node`, and the component's own files
are only the ones listed below.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/work-browser:0.1.0 / /
RUN useradd --create-home --uid 1000 --shell /bin/bash me
ENV MALRUNAR_WORK_BROWSER_USER=me
```

That is a working work browser: copying the component in adds the supervisord program
`work-browser` (`/etc/supervisor/conf.d/malrunar-work-browser.conf`, the
[supervisor.d convention](../CONVENTIONS.md#supervisord-programs-of-components)), which `supervisord`
in `desktop-base` includes from `conf.d`. Its program does not name a user, a path or a site; all of
that is the environment variables below. [`share/Dockerfile.example`](share/Dockerfile.example) is a
longer example with policies and preferences. To turn the program off in one image, delete that
file in your layer, or set `MALRUNAR_WORK_BROWSER_ENABLED=0` (the launcher then exits at once).

| Path | What it is |
|---|---|
| `/opt/malrunar/work-browser/bin/malrunar-work-browser` | The launcher, run by supervisord |
| `/opt/malrunar/work-browser/bin/malrunar-work-browser-open` | Opens a URL as a new tab (or window) in the running browser |
| `/opt/malrunar/work-browser/lib/common.sh` | Functions shared by the two scripts |
| `/opt/malrunar/work-browser/share/` | `user.js.example`, `policies.json.example`, `Dockerfile.example` |
| `/etc/supervisor/conf.d/malrunar-work-browser.conf` | The supervisord program `work-browser` |

Nothing is added to `PATH`; call the scripts by their path. A desktop shortcut can use
`/opt/malrunar/work-browser/bin/malrunar-work-browser-open "%u"`.

## Configuration

All variables are optional except the user when the program starts as root (supervisord does).

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_WORK_BROWSER_USER` | none | The user that owns the browser. The launcher is started as root by supervisord and switches to this user (`setpriv`); it exits with status 78 and a message if it is unset (as root) or does not exist |
| `MALRUNAR_WORK_BROWSER_FIREFOX` | `/opt/malrunar/firefox/firefox` | The Firefox to run |
| `MALRUNAR_WORK_BROWSER_PROFILE_DIR` | `$HOME/.config/malrunar-work-browser/profile` | The persistent profile. Mount a volume here to keep it |
| `MALRUNAR_WORK_BROWSER_USER_JS` | none | A file installed as `user.js` in the profile on every start: your preferences |
| `MALRUNAR_WORK_BROWSER_PROFILE_SEED` | none | A directory copied into the profile at start without overwriting existing files (`chrome/`, extensions you add, and so on) |
| `MALRUNAR_WORK_BROWSER_START_URLS` | none | Space-separated URLs opened at start. Leave it empty to let Firefox restore its session (see `user.js.example`) |
| `MALRUNAR_WORK_BROWSER_ARGS` | none | Extra Firefox arguments, split on whitespace (for example `--headless`) |
| `MALRUNAR_WORK_BROWSER_DISPLAY` | the X display that answers | A fixed `DISPLAY`, or `none` to run without one |
| `MALRUNAR_WORK_BROWSER_DBUS` | `private` | `private`: a session bus of its own at `<runtime dir>/bus`, shared by the launcher and the open helper; `inherit`: use the environment's bus; `none`: no bus |
| `MALRUNAR_WORK_BROWSER_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/malrunar-work-browser-<uid>` (`/tmp/...` without `XDG_RUNTIME_DIR`) | Where the private bus socket lives (mode 700) |
| `MALRUNAR_WORK_BROWSER_PID_FILE` | none | Write the Firefox pid here while it runs |
| `MALRUNAR_WORK_BROWSER_RETRY_SECONDS` | `10` | Wait before looking for a display again, and before restarting Firefox after it exits |
| `MALRUNAR_WORK_BROWSER_ENABLED` | `1` | `0` makes the launcher exit with status 0 without starting Firefox |
| `MALRUNAR_WORK_BROWSER_OPEN_MODE` | `tab` | For the open helper: `tab` or `window` |
| `MALRUNAR_WORK_BROWSER_OPEN_TIMEOUT` | `15` | For the open helper: seconds before the handoff is abandoned |

supervisord passes its own environment to the program, so `docker run -e`, compose `environment:`
or `ENV` in your image are all that is needed.

### What the launcher does

1. Switches to `MALRUNAR_WORK_BROWSER_USER` if it started as root.
2. Waits for a working X display (unless `MALRUNAR_WORK_BROWSER_DISPLAY=none`).
3. Prepares the profile: creates it (mode 700), copies the seed, installs `user.js`.
4. Makes sure the session bus Firefox uses to hand URLs to a running instance is reachable. Without
   one, `malrunar-work-browser-open` would hang.
5. Clears a stale profile lock and stops leftover Firefox processes on the profile that do not hold it.
6. Runs `firefox -profile <dir> -new-instance ...` and, when it exits, runs it again after the retry delay.

`malrunar-work-browser-open` exits with status 3 when no browser holds the profile (instead of
starting an unsupervised second one) and with status 78 when the image or a setting cannot work.

## Firefox stays unmodified

Mozilla's distribution policy makes a Firefox with changed policies, extensions or preferences a
modified build with obligations that are yours. `desktop-base` therefore ships Firefox exactly as
Mozilla builds it, and this component adds no policy file, distribution directory, autoconfig, extension
or preference to it (the smoke test checks that, and that the installation still matches the checksums
recorded at build time). Everything that configures the browser comes from **your** layers:

- Policies: put `policies.json` where Firefox on Linux reads system policies,
  `/etc/firefox/policies/policies.json` (or `/opt/malrunar/firefox/distribution/policies.json`).
  [`share/policies.json.example`](share/policies.json.example) is a harmless example.
- Preferences: a `user.js` file named by `MALRUNAR_WORK_BROWSER_USER_JS`.
- Profile content: a directory named by `MALRUNAR_WORK_BROWSER_PROFILE_SEED`.

## Not included

Sign-in automation, extensions, camera and microphone permissions and anything specific to a site or
account belong in your own image. The [`media-bridge`](../media-bridge/) component's
`bin/malrunar-media-profile` prints a policy that limits camera and microphone to named sites, which
you can use as your `policies.json`.
