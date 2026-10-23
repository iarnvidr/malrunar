# desktop-wrapper

A native desktop window around one website, for macOS, with native notifications. It is a small
[Tauri v2](https://v2.tauri.app/) app. The site is not bundled: the window loads the live address,
so deploying the site updates the app (relaunch it). What it is called, where it points, its bundle
id and its icon come from a configuration file or environment variables at build time. Nothing about
a particular site is built into the source.

Use it when you want a web app (a mail client, a call page, a dashboard) in its own Dock icon and
window, with its own sign-in session and notifications that arrive while the window is closed.

## What it adds to a browser tab

- A window of its own with its own Dock icon. The web view keeps its data (cookies, local storage)
  in the per-user app data directory, so a sign-in survives restarts.
- One instance only: launching again focuses the open window. Window size and position are remembered.
- Native notifications for the page's `Notification` calls, and a Dock badge for
  `navigator.setAppBadge` (see [Notifications](#notifications)).
- Links to other sites open in the system browser; the site and the sign-in hosts you name stay in
  the app (see `internal_hosts`).
- Downloads go to your Downloads folder, and the file is revealed when it finishes.
- When the site cannot be reached the app shows a Retry page (and retries every 15 seconds) instead
  of a blank window.
- On macOS, closing the window leaves the app running in the Dock so notifications keep arriving
  (`hide_on_close`). Click the Dock icon to bring the window back, Cmd+Q to quit.
- Camera and microphone: the page may ask for them (the usage descriptions are in the generated
  `Info.plist`); macOS then asks you once.

## Configuration

A JSON file. The default is `wrapper.config.json` in this directory (create it; it is not tracked),
falling back to `wrapper.config.example.json`. `WRAPPER_CONFIG=/path/to/file.json` or
`scripts/configure.py --config FILE` picks another one.

```json
{
  "name": "Example App",
  "id": "example-app",
  "url": "https://app.example.com/",
  "version": "0.1.0",
  "icon": "icon.png",
  "internal_hosts": ["login.example.com", "*.idp.example.net"],
  "window": { "width": 1280, "height": 860, "min_width": 480, "min_height": 600 },
  "notifications": true,
  "hide_on_close": true
}
```

| Setting | Required | Meaning |
|---|---|---|
| `name` | yes | The name shown in the Dock, menus, window title and notifications. 1 to 40 characters: letters, digits, spaces and `. _ ' -`. |
| `id` | yes | Lower case letters, digits and hyphens (2 to 40, starting with a letter). The bundle id is `io.github.iarnvidr.<id>`. |
| `url` | yes | The site. `https://` only; `http://` is accepted for `localhost`, `127.0.0.1` and `[::1]` (testing). No user name or password. |
| `version` | no | `1.2.3`, default `0.1.0`. |
| `identifier_prefix` | no | Replaces `io.github.iarnvidr` in the bundle id (a fork uses its own, for example `io.github.yourname`). |
| `icon` | no | A square PNG of at least 512 pixels (1024 recommended, with transparency for rounded corners) or an SVG, relative to the configuration file. Without it the neutral icon in `src-tauri/icons/` (made from `default-icon.svg`) is used. |
| `internal_hosts` | no | Further hosts that load in the app window, for example a sign-in provider: `login.example.com`, or `*.idp.example.net` for every subdomain (not the bare domain). https on the default port only. Everything else opens in the system browser. |
| `window` | no | Initial and minimum size in points. |
| `notifications` | no | `false` leaves the page without the notification and badge bridge. Default `true`. |
| `hide_on_close` | no | macOS: closing the window hides it instead of quitting. Default `true`. |

Environment variables override the file, which is how CI and `workflow_dispatch` build different
apps from the same source: `WRAPPER_NAME`, `WRAPPER_ID`, `WRAPPER_IDENTIFIER_PREFIX`, `WRAPPER_URL`,
`WRAPPER_VERSION`, `WRAPPER_ICON`. Empty variables are ignored.

`scripts/configure.py` validates all of it (a wrong value stops the build with a message that names
the setting) and writes, under `src-tauri/`, the files the build uses: `generated/app.json` (read
by the app), `generated/tauri.overlay.json` (product name, version, bundle id, icons for the Tauri
bundler), `generated/icons/` and `Info.plist`. None of them is tracked.

```sh
python3 scripts/configure.py --check    # validate and print the normalised settings
```

## Build locally

On a Mac with the Xcode command line tools, Node 22 and Rust. The pinned toolchain
(`rust-toolchain.toml`) is installed by `rustup`; the Tauri CLI is pinned in `package.json` and
`package-lock.json`, the crates in `src-tauri/Cargo.lock`.

```sh
cd apps/desktop-wrapper
cp wrapper.config.example.json wrapper.config.json     # then edit it
npm ci
rustup target add aarch64-apple-darwin x86_64-apple-darwin
npm run dev                                                           # run against the site
npm run build -- --target universal-apple-darwin --bundles app,dmg    # Apple Silicon and Intel
# or one architecture, for example: npm run build -- --bundles app,dmg
```

The bundle is `src-tauri/target/<target>/release/bundle/macos/<name>.app` and `.../dmg/`. The
build is ad-hoc signed (`codesign -s -`), which is enough to launch on Apple Silicon. Without a Mac,
use the GitHub workflow below.

## Build on GitHub

[`.github/workflows/desktop-wrapper.yml`](../../.github/workflows/desktop-wrapper.yml) builds on
GitHub-hosted `macos-15` runners (Apple Silicon). Standard macOS runners are free for public
repositories, so this needs no Mac and no paid minutes. A universal build and disk image takes
about 10 minutes (the first pull request run took 9 minutes 40 seconds with a cold cache).

- **Pull requests** that touch `apps/desktop-wrapper/**` run the Linux tests, then build the
  example configuration pointed at a stand-in site on `127.0.0.1`, verify the bundle, and launch
  the real app against that stand-in site: the page loads in the web view and its `new Notification`
  and `setAppBadge` calls must reach the app. The result is uploaded as an artifact kept for 3 days.
- **Run workflow** (workflow_dispatch) builds the app for the name, address, id, version and icon
  you give it (a fork can put its `wrapper.config.json` in the repository instead and give only
  the address). The artifact `desktop-wrapper-macos` holds a `.zip` of the `.app`, a `.dmg` and
  `SHA256SUMS`, kept for 30 days. Nothing is published and no secret is used.

## Opening an unsigned build

There is no Apple Developer certificate, so the app is ad-hoc signed and not notarised. Gatekeeper
treats a downloaded copy as coming from an unidentified developer.

1. Unzip, and drag the app to Applications (or open the disk image and drag it from there).
2. First launch: right-click the app, choose **Open**, then **Open** again.
   Or remove the quarantine flag once: `xattr -dr com.apple.quarantine "/Applications/<name>.app"`.
3. On macOS 15 and later, if the right-click route only offers "Move to Trash": System Settings,
   Privacy and Security, scroll to the message about the app, **Open Anyway**.

An app built on your own Mac is not quarantined and opens normally. To distribute without this
dance you need a Developer ID certificate and notarisation, which this repository does not have;
add `signingIdentity` and notarisation settings to `src-tauri/tauri.conf.json` in your own fork.

## Notifications

A desktop web view has no Notification permission prompt and no Web Push. The app injects, before
the page's own scripts, a standard `Notification` class and `navigator.setAppBadge`/`clearAppBadge`.
A page that already uses the web API needs no change:

```js
new Notification('New message', { body: 'From Sam' });   // a native notification
navigator.setAppBadge(3);                                  // Dock badge "3"
```

- `Notification.permission` is always `granted` and `requestPermission()` resolves `granted`.
  macOS itself decides whether banners show: allow notifications for the app in System Settings,
  Notifications when it asks (macOS asks on the first one).
- Title and body are plain text, control characters are removed, long text is cut (120 and 500
  characters) and at most 10 notifications are shown in any 10 seconds.
- `onshow`, `onclick`, `onerror`, `onclose` and the matching events work. Clicking the banner brings
  the window to the front and fires `click` on that notification object (the page decides what to
  open). `tag`, `icon`, `actions` and `requireInteraction` are ignored.
- Only the commands `wrapper_notify` and `wrapper_badge` are callable from the page, and only from
  the configured site's origin (`src-tauri/src/main.rs`, `commands.rs`); pages from other hosts get
  nothing.
- Windows and Linux builds show plain notifications (no click) and have no badge, but only macOS
  is built and tested by the workflow.

## Limits

- Notifications come from the page while the app is running: the page must be open (hiding the
  window with the close button is fine; background throttling is off). Quit the app and nothing
  arrives; notifications are not queued and Web Push is not supported. A site that only notifies
  through Web Push or a service worker (`registration.showNotification`) will not notify here.
- The page keeps no offline copy: with no connection you get the Retry page.
- macOS notifications use `mac-notification-sys`, which uses the system API directly. If a future
  macOS release stops delivering them, `src-tauri/src/notify.rs` is the one file to move to
  `UNUserNotificationCenter`.
- Passkeys and security keys depend on the platform web view and may not prompt; use another second
  factor if a prompt does not appear.
- Unsigned builds have no auto-update. Rebuild and replace the app to update the shell itself.
- Sites that detect embedded web views may behave differently from Safari.

## Tests

```sh
cd apps/desktop-wrapper
npm ci
npm test                                         # configuration, injected scripts, stand-in site
cargo test --locked --manifest-path src-tauri/Cargo.toml -p desktop-wrapper-core
```

- `scripts/test_configure.py`: validation of every setting (valid and invalid values), environment
  overrides, the generated files, and that the Rust crate's fixture is what the script produces.
- `src-tauri/core`: the Rust side of the configuration (strict parsing), the navigation policy
  (look-alike hosts, ports, schemes), notification text and rate limits, safe download names.
- `tests/js/shims.test.mjs`: the injected `Notification`, badge and link scripts against fakes.
- `tests/e2e/`: on macOS, `verify-macos.sh` checks the bundle (identifier, architectures, usage
  descriptions, signature) and `e2e-macos.sh` launches the app against `server.py`. The
  workflow runs both on pull requests; the Linux part runs on every change here.

Run `scripts/check-public-hygiene.sh` before sending a change: this repository is public and the
app is generic.

## Layout

```
wrapper.config.example.json   the settings, with an example
scripts/configure.py          validates them and writes the build inputs
src-tauri/core/               GUI-free Rust: config, navigation policy, notification limits, file names
src-tauri/src/                the app: window, links, downloads, notification commands
src-tauri/scripts/            what is injected into the page (links.js, notifications.js)
ui/                           the local connection-check page
```
