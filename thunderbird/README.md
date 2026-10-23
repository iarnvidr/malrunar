# thunderbird

Mozilla's official **Thunderbird, unmodified**, as a component for a desktop image: the release
tarball, verified, unpacked in `/opt/malrunar/thunderbird/app`, and a launcher that keeps it running on
a persistent profile under supervisord. It is for a desktop whose mailbox a person, or an agent through
[`thunderbird-mcp`](../thunderbird-mcp/), works in.

The component is `FROM scratch`. Nothing in it is added to Thunderbird: no policy, add-on, preference
or patch (see [What is modified, and where](#what-is-modified-and-where)).

## Dependencies

| Needs | Why |
|---|---|
| [`desktop-base`](../desktop-base/) | the libraries Thunderbird links against (GTK 3, NSS, ALSA, dbus-glib, libXt and the X11 libraries), `python3` for the consumer tools, `xdpyinfo`, `setpriv`, supervisord |
| a desktop user | created in your own layer; named in `MALRUNAR_THUNDERBIRD_USER` |
| a desktop session | an X display to show the window on (your xrdp session); the launcher waits for it, or runs headless with `MALRUNAR_THUNDERBIRD_DISPLAY=none` |

It needs **no other component**. In particular it does not need `node` (only the optional
[`thunderbird-mcp`](../thunderbird-mcp/) bridge does), `work-browser` or `session-kit`. The smoke test
enforces this: without `desktop-base` the launcher exits with status 78 and a message naming it, and the
test image holds `desktop-base` and this component only.

## Compose

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/thunderbird:158.0.1 / /
RUN useradd --create-home --uid 1000 --shell /bin/bash me
ENV MALRUNAR_THUNDERBIRD_USER=me
```

That is a working Thunderbird: copying the component in adds the supervisord program `thunderbird`
(`/etc/supervisor/conf.d/malrunar-thunderbird.conf`, the
[supervisor.d convention](../CONVENTIONS.md#supervisord-programs-of-components)), which `supervisord` in
`desktop-base` includes from `conf.d`. The fragment names no user, path or site; everything is the
environment variables below. To turn the program off in one image, delete that file in your layer, or set
`MALRUNAR_THUNDERBIRD_ENABLED=0` (the launcher then exits at once). Mount a volume at the profile
directory to keep the mailbox.

| Path | What it is |
|---|---|
| `/opt/malrunar/thunderbird/app/` | Thunderbird as Mozilla ships it (`app/thunderbird` is the program) |
| `/opt/malrunar/thunderbird/unmodified.sha256` | sha256 of every file of `app/` as built; `cd app && sha256sum -c ../unmodified.sha256` |
| `/opt/malrunar/thunderbird/bin/malrunar-thunderbird` | The launcher, run by supervisord |
| `/opt/malrunar/thunderbird/consumer/` | What a consumer may apply to its own copy at its own build, see below |
| `/opt/malrunar/thunderbird/SOURCE.txt`, `LICENSE-MPL-2.0` | Where the release and its source come from; the Mozilla Public License 2.0 |
| `/etc/supervisor/conf.d/malrunar-thunderbird.conf` | The supervisord program `thunderbird` |

Nothing is added to `PATH`; call `/opt/malrunar/thunderbird/app/thunderbird`.

## Configuration

All variables are optional except the user when the program starts as root (supervisord does).

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_THUNDERBIRD_USER` | none | The user that owns the profile. The launcher is started as root by supervisord and switches to this user (`setpriv`); it exits with status 78 and a message if it is unset (as root) or does not exist |
| `MALRUNAR_THUNDERBIRD_APP` | `/opt/malrunar/thunderbird/app` | The Thunderbird directory to run (and that `consumer/customize.py` works on) |
| `MALRUNAR_THUNDERBIRD_PROFILE_DIR` | `$HOME/.config/malrunar-thunderbird/profile` | The profile. Mount a volume here to keep it. Give it as a path outside the standard profile root, and never a profile Thunderbird already uses under another path |
| `MALRUNAR_THUNDERBIRD_USER_JS` | none | A file installed as `user.js` in the profile on every start: your preferences |
| `MALRUNAR_THUNDERBIRD_ARGS` | none | Extra Thunderbird arguments, split on whitespace |
| `MALRUNAR_THUNDERBIRD_DISPLAY` | the X display that answers | A fixed `DISPLAY`, or `none` to run headless (`MOZ_HEADLESS=1`) |
| `MALRUNAR_THUNDERBIRD_PID_FILE` | none | Write the Thunderbird pid here while it runs |
| `MALRUNAR_THUNDERBIRD_RETRY_SECONDS` | `10` | Wait before looking for a display again, and before restarting Thunderbird after it exits |
| `MALRUNAR_THUNDERBIRD_ENABLED` | `1` | `0` makes the launcher exit with status 0 without starting Thunderbird |

supervisord passes its own environment to the program, so `docker run -e`, compose `environment:` or
`ENV` in your image are all that is needed.

### What the launcher does

1. Switches to `MALRUNAR_THUNDERBIRD_USER` if it started as root.
2. Checks the libraries of `desktop-base` are there (status 78 and a message naming it if not).
3. Waits for a working X display (unless `MALRUNAR_THUNDERBIRD_DISPLAY=none`).
4. Creates the profile (mode 700), installs `user.js`, clears a stale lock whose owner is gone.
5. Runs `thunderbird --no-remote --profile <dir> ...` and, when it exits, runs it again after the retry delay.

## What is modified, and where

Mozilla's [distribution policy](https://www.mozilla.org/foundation/trademarks/distribution-policy/) lets
the Thunderbird name be used on an unaltered build of the latest stable release, and asks for permission
for anything else (adding add-ons or policies, changing preferences, editing files). So:

- **This image is the unaltered release.** It is downloaded from `archive.mozilla.org`, checked against
  the release's PGP-signed `SHA256SUMS` (the signing key is committed, its fingerprint pinned) and against
  the pinned sha256, and unpacked as it is. The version tracks the latest stable release (the weekly pin
  update moves it).
- **Changes are applied at your build**, to your copy, by the tools in `consumer/`. The result is a
  modified Thunderbird, and distributing it under Mozilla's name is a question between you and Mozilla.
  For your own use, which is what these images are for, there is nothing to ask.

### Consumer-side changes (`consumer/`)

`consumer/customize.py` needs only the `python3` of `desktop-base`. In your Dockerfile, after copying the
component in:

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/thunderbird:158.0.1 / /
ARG TB=/opt/malrunar/thunderbird/consumer
RUN python3 $TB/customize.py oauth-shared-mailbox \
 && python3 $TB/customize.py addon-pinned tbsync \
 && python3 $TB/customize.py addon-pinned eas4tbsync \
 && python3 $TB/customize.py policies $TB/policies/maildir-and-oauth.json $TB/policies/tbsync.json \
 && python3 $TB/customize.py verify
```

Every command is independent and optional. Each one that changes the installation appends a line to
`/opt/malrunar/thunderbird/MODIFIED.txt`, so an image says it is modified.

| Command | What it does |
|---|---|
| `oauth-shared-mailbox` | Patches Mozilla's `modules/OAuth2Module.sys.mjs` inside `omni.ja` so a delegated (shared) IMAP mailbox can sign in with the refresh token of the account that owns it: the server user name stays the mailbox in the XOAUTH2 string, and a per-server preference `oauth2.username` names the login that holds the token. Every replacement is checked against the exact upstream text and the build fails if a Thunderbird release changed it. The patch is `consumer/oauth_shared_mailbox.py` and is offered under MPL-2.0 (it modifies an MPL file; [LICENSE-MPL-2.0](LICENSE-MPL-2.0)). Delete the profile's `startupCache` directory after applying it to an existing profile |
| `addon NAME URL SHA256` | Downloads an add-on over https, checks its sha256, raises `strict_max_version` in its manifest (to the installed major version, for example `158.*`, or `--max-version`) and writes `/opt/malrunar/thunderbird-addons/NAME.xpi` (`--dir` or `MALRUNAR_THUNDERBIRD_ADDON_DIR`). It is an override of the add-on's own compatibility range, not a guarantee that it works on that release |
| `addon-pinned tbsync` / `eas4tbsync` | The same for the two add-ons pinned in `consumer/addons.json`: TbSync and its Provider for Exchange ActiveSync (both MPL-2.0, from addons.thunderbird.net). They are not in any malrunar image |
| `policies FILE...` | Deep-merges JSON policy fragments into `app/distribution/policies.json`. `consumer/policies/maildir-and-oauth.json` locks new accounts to the Maildir store and keeps Microsoft sign-in inside Thunderbird's own window; `consumer/policies/tbsync.json` installs the two add-ons above. Other components ship their own fragments (`thunderbird-mcp` has `share/policies-extension.json.example`) |
| `verify` | Checks `omni.ja` is a sound zip and the policy file is valid JSON |

CI applies all of this to a copy of the unmodified Thunderbird in a throwaway image (never published),
checks that only `omni.ja` and `distribution/policies.json` differ from the release, runs the OAuth test
(`test/oauth-shared-mailbox.test.mjs`, which also fails on the unmodified module) and starts the result,
which installs both add-ons from its policy.

## Not included

Sign-in flows, account and profile provisioning, and anything specific to a site or mailbox belong in your
own image. Thunderbird's security updates arrive with the pin update: rebuild and republish the image you
derive from it when the `thunderbird` version moves.
