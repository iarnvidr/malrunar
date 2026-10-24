# obs-control: configuration, HTTP API and plugins

`obs-control` is OBS Studio running headless in a container (on top of `obs-runtime`) with an HTTP
API in front of it. It is meant to be one box in a larger set-up: a browser source (a web page that
receives a camera, a dashboard, any URL) and a media scene are composed in OBS, the program output
is streamed as MPEG-TS over UDP, and the API lets another program switch scenes, play and record
clips, watch a preview and start or stop the output.

```
 camera page (any URL)        HTTP API (:18095)  <---- your control program / browser console
        |                         |
        v                         v
 +--------------------- obs-control container -----------------------+
 |  OBS (Xvfb, browser source, media source)  <-- obs-websocket --    |
 |        | MPEG-TS over UDP                    controller (aiohttp)   |
 +--------|-----------------------------------------------------------+
          v
   media-bridge  ->  virtual camera (v4l2loopback) + virtual microphone (PipeWire)  in a desktop-base container
```

The scene collection is generated from a declarative configuration before OBS starts. Nothing in
the image is specific to one site or product: scene names, source names, the browser-source URL,
feature switches and the identities that may use the API are all configuration. A plugin hook lets
a consumer add routes and scenes in its own layer.

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Modes and feature switches](#modes-and-feature-switches)
- [HTTP API and compatibility table](#http-api-and-compatibility-table)
- [Identity and security](#identity-and-security)
- [Plugins](#plugins)
- [Takes (built-in plugin)](#takes-built-in-plugin)
- [The UDP relay](#the-udp-relay)
- [Files, state and restarts](#files-state-and-restarts)
- [Migrating from the previous in-product controller](#migrating-from-the-previous-in-product-controller)

## Quick start

The three images side by side (a runnable version of this is `obs-control/test/e2e/`):

```sh
docker network create desk
# 1. the desktop that will use the camera and microphone: desktop-base + media-bridge
#    (see ../README.md and ../media-bridge/README.md; its bridge listens on UDP 5004)
# 2. OBS, sending its program to the bridge:
docker run -d --name obs --network desk \
  -e MALRUNAR_OBS_CAMERA_URL='https://example.invalid/camera-page' \
  -e MALRUNAR_OBS_OUTPUT_URL='udp://desktop:5004?pkt_size=1316' \
  -v obs-config:/config -p 127.0.0.1:18095:18095 \
  --shm-size 512m ghcr.io/iarnvidr/obs-control:0.1.0
curl -fsS http://127.0.0.1:18095/ready          # {"ready": true, "obsVersion": "32.2.0", ...}
curl -fsS -X POST http://127.0.0.1:18095/api/scene/live
curl -fsS -o preview.jpg http://127.0.0.1:18095/api/program-preview.jpg
```

Open `http://127.0.0.1:18095/` for the built-in console. A configuration file is usually clearer
than a long list of variables: mount one at `/etc/malrunar/obs-control.toml` (see
`/usr/local/share/malrunar/obs-control/examples/obs-control.example.toml` in the image, and
`obs-control/examples/` in the repository). `entrypoint.sh check` validates a configuration
and exits; the image also contains `examples/test-card.html`, a moving picture with a tone, for a
first try (`file:///usr/local/share/malrunar/obs-control/examples/test-card.html`).

Give the container `--shm-size 512m` (Chromium, which renders browser sources, needs shared memory)
and a volume at `/config` that the user `obs` (uid/gid 1000) can write.

## Configuration

Precedence, lowest to highest: built-in defaults, the config file, `MALRUNAR_OBS_*` environment
variables. The file is TOML (`.toml`) or JSON (`.json`); its path is `MALRUNAR_OBS_CONFIG`, else
`/etc/malrunar/obs-control.toml` if it exists. Unknown sections or keys, wrong types and duplicate
scene or source names are errors that name the setting; the container does not start with an invalid
configuration.

Every scalar `[section] key` has an environment variable `MALRUNAR_OBS_<SECTION>_<KEY>` (for
`[obs]`: `MALRUNAR_OBS_<KEY>`); booleans are `true/false/1/0/yes/no/on/off`, lists are
comma-separated. The first `[[cameras]]` entry is set with `MALRUNAR_OBS_CAMERA_<KEY>`. A few
short aliases exist and are listed with the key they set. `[[cameras]]` replaces the default list
when it appears in a file; `[profiles.*]` replaces the default profiles; `[plugin.<name>]` is free
for plugins.

Settings read only from the environment: `MALRUNAR_OBS_CONFIG` (config file path) and
`MALRUNAR_OBS_RESTORE` (a JSON object, see [Files, state and restarts](#files-state-and-restarts)).
`entrypoint.sh` also reads `DISPLAY` (`:99`), `XDG_RUNTIME_DIR` (`/tmp/obs-runtime`) and
`MALRUNAR_OBS_XVFB_SCREEN` (`1280x720x24`).

### Reference

#### `[obs]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `config_dir` | (empty) | `MALRUNAR_OBS_CONFIG_DIR` | OBS config directory; must end in `/obs-studio`. Empty: `$XDG_CONFIG_HOME/obs-studio`, else `$HOME/.config/obs-studio` (the image: `/config/.config/obs-studio`) |
| `profile` | `"malrunar"` | `MALRUNAR_OBS_PROFILE` | OBS profile name |
| `collection` | `"malrunar"` | `MALRUNAR_OBS_COLLECTION` | OBS scene collection name |
| `websocket_port` | `4455` | `MALRUNAR_OBS_WEBSOCKET_PORT` | Port of the obs-websocket server inside the container |
| `websocket_password` | (empty) | `MALRUNAR_OBS_WEBSOCKET_PASSWORD` | obs-websocket password. Empty: a random one is generated on first start and kept in the OBS config |
| `websocket_auth` | `true` | `MALRUNAR_OBS_WEBSOCKET_AUTH` | Require the obs-websocket password (the server listens on all interfaces of the container; keep this on) |
| `start_scene` | (empty) | `MALRUNAR_OBS_START_SCENE` | Scene on air at start: a scene key, a scene name or `stopped`. Empty: the first camera, or the stopped scene when `program_control` is on |

#### `[http]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `bind` | `"0.0.0.0"` | `MALRUNAR_OBS_HTTP_BIND`, `MALRUNAR_OBS_CONTROL_BIND` | Address the HTTP API listens on |
| `port` | `18095` | `MALRUNAR_OBS_HTTP_PORT`, `MALRUNAR_OBS_CONTROL_PORT` | Port of the HTTP API |

#### `[video]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `width` | `640` | `MALRUNAR_OBS_VIDEO_WIDTH` | OBS canvas and output width |
| `height` | `360` | `MALRUNAR_OBS_VIDEO_HEIGHT` | OBS canvas and output height |
| `fps` | `24` | `MALRUNAR_OBS_VIDEO_FPS` | Frame rate |
| `default_profile` | `"360p"` | `MALRUNAR_OBS_VIDEO_DEFAULT_PROFILE` | The `[profiles.*]` entry applied at start in program control with `video_profiles` |

#### `[output]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `url` | `"udp://127.0.0.1:18096?pkt_size=1316"` | `MALRUNAR_OBS_OUTPUT_URL` | Where OBS sends the program: MPEG-TS over UDP (H.264 + AAC), for example the media-bridge container |
| `video_bitrate_kbps` | `1200` | `MALRUNAR_OBS_OUTPUT_VIDEO_BITRATE_KBPS` | H.264 bitrate in kbit/s |
| `audio_bitrate_kbps` | `128` | `MALRUNAR_OBS_OUTPUT_AUDIO_BITRATE_KBPS` | AAC bitrate in kbit/s |
| `gop` | `48` | `MALRUNAR_OBS_OUTPUT_GOP` | Keyframe interval in frames |
| `x264_preset` | `"veryfast"` | `MALRUNAR_OBS_OUTPUT_X264_PRESET` | x264 preset |
| `rescale` | (empty) | `MALRUNAR_OBS_OUTPUT_RESCALE` | `WxH` to scale the output independently of the canvas; empty: no rescale |
| `autostart` | `true` | `MALRUNAR_OBS_OUTPUT_AUTOSTART` | Start the output when OBS has started (retried for three minutes while the sources produce their first frame) |

#### `[media]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `key` | `"video"` | `MALRUNAR_OBS_MEDIA_KEY` | Scene key of the media scene in `POST /api/scene/<key>` |
| `scene` | `"B-roll Video"` | `MALRUNAR_OBS_MEDIA_SCENE` | Media (b-roll) scene name |
| `source` | `"B-roll Source"` | `MALRUNAR_OBS_MEDIA_SOURCE` | Media source name |
| `file` | (empty) | `MALRUNAR_OBS_MEDIA_FILE`, `MALRUNAR_OBS_MEDIA_FILE` | A video file looped until automatic b-roll replaces it; empty: none |

#### `[stopped]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `scene` | `"Stopped"` | `MALRUNAR_OBS_STOPPED_SCENE` | Neutral scene name |
| `source` | `"Stopped Frame"` | `MALRUNAR_OBS_STOPPED_SOURCE` | Solid-colour source name |
| `color` | `"#000000"` | `MALRUNAR_OBS_STOPPED_COLOR` | Colour, `#rrggbb` |

#### `[clips]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `scene_prefix` | `"Recorded"` | `MALRUNAR_OBS_CLIPS_SCENE_PREFIX` | Clip scenes are named `<prefix> · <clip name>` |
| `source_prefix` | `"Recorded Source"` | `MALRUNAR_OBS_CLIPS_SOURCE_PREFIX` | Clip sources are named `<prefix> · <clip id>` |

#### `[features]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `program_control` | `false` | `MALRUNAR_OBS_FEATURES_PROGRAM_CONTROL` | Program control: stopped scene, `/api/call/*` gates, clip playback and deletion, input levels, sender-lease supervision, explicit b-roll capture |
| `video_profiles` | `false` | `MALRUNAR_OBS_FEATURES_VIDEO_PROFILES` | Enable `POST /api/call/video-profile` and apply a profile at start (needs `program_control`) |
| `sender_lease` | `true` | `MALRUNAR_OBS_FEATURES_SENDER_LEASE` | Require sender heartbeats. Off: the sender is always considered present |
| `mode_label` | `"program-control"` | `MALRUNAR_OBS_FEATURES_MODE_LABEL` | The `mode` value `/ready` reports in program control |
| `takes` | `false` | `MALRUNAR_OBS_FEATURES_TAKES` | Load the built-in [takes plugin](#takes-built-in-plugin) (same as `plugins.modules = ["malrunar_obs.takes"]`). Off: its routes, scenes and profile settings do not exist |

#### `[sender]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `lease_seconds` | `12.0` | `MALRUNAR_OBS_SENDER_LEASE_SECONDS` | A heartbeat keeps the sender alive for this long |

#### `[broll]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `enabled` | `true` | `MALRUNAR_OBS_BROLL_ENABLED` | Automatic b-roll on/off |
| `auto` | `true` | `MALRUNAR_OBS_BROLL_AUTO` | Start b-roll capture by itself (on heartbeat, or at start without `sender_lease`); not used in program control, where `POST /api/daily-fallback/ensure` starts it |
| `seconds` | `180` | `MALRUNAR_OBS_BROLL_SECONDS` | Length of the finished video (at least 10) |
| `chunk_seconds` | `90` | `MALRUNAR_OBS_BROLL_CHUNK_SECONDS` | Capture chunk length (at least 30) |
| `max_seconds` | `1200` | `MALRUNAR_OBS_BROLL_MAX_SECONDS` | Give up collecting after this many captured seconds |
| `silence_db` | `-38.0` | `MALRUNAR_OBS_BROLL_SILENCE_DB` | Level below which audio counts as quiet (dB) |
| `motion_threshold` | `0.025` | `MALRUNAR_OBS_BROLL_MOTION_THRESHOLD` | Scene-change score below which the picture counts as steady |
| `min_segment_seconds` | `6.0` | `MALRUNAR_OBS_BROLL_MIN_SEGMENT_SECONDS` | Shortest quiet and steady span worth keeping (at least 3) |
| `preview_url` | (empty) | `MALRUNAR_OBS_BROLL_PREVIEW_URL` | The `url` the b-roll status reports; empty: `api/daily-fallback/preview` in program control, `media/3-minute-video.mp4` otherwise |

#### `[access]`

Authentication is external (an authenticating reverse proxy such as Authelia; nothing here is tied to a product).
These settings say which requests may be believed when they carry identity headers, and which groups may do
what. See [Identity and security](#identity-and-security).

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `identity_header` | (empty: `Remote-User`) | `MALRUNAR_OBS_ACCESS_IDENTITY_HEADER` | Header with the user name. Setting it switches identities on (per-user b-roll in program control) but trusts nothing by itself: see `trusted_proxies` and `proxy_secret_header` |
| `groups_header` | (empty: `Remote-Groups`) | `MALRUNAR_OBS_ACCESS_GROUPS_HEADER` | Header with the user's groups, comma separated |
| `name_header` | (empty: `Remote-Name`) | `MALRUNAR_OBS_ACCESS_NAME_HEADER` | Header with the display name |
| `email_header` | (empty: `Remote-Email`) | `MALRUNAR_OBS_ACCESS_EMAIL_HEADER` | Header with the mail address |
| `trusted_proxies` | `[]` | `MALRUNAR_OBS_ACCESS_TRUSTED_PROXIES` | List of addresses or CIDRs (IPv4 and IPv6; the environment variable is comma separated) the connection's peer address must be in. The address of the TCP peer is used, never `X-Forwarded-For` |
| `proxy_secret_header` | (empty) | `MALRUNAR_OBS_ACCESS_PROXY_SECRET_HEADER` | Header in which the proxy sends a shared secret. Set together with `proxy_secret_file` |
| `proxy_secret_file` | (empty) | `MALRUNAR_OBS_ACCESS_PROXY_SECRET_FILE` | File with the secret: one per line, at most two (old and new during a rotation), each at least 16 characters; `#` lines and blank lines are ignored. Re-read when its modification time changes. The secret is never in the config file, logs or responses |
| `groups` (table `[access.groups]`) | `{}` | `MALRUNAR_OBS_ACCESS_GROUPS_VIEWER`, `MALRUNAR_OBS_ACCESS_GROUPS_OPERATOR` | Capability to list of groups: `operator = ["calls-operators"]`, `viewer = ["calls-viewers"]` (environment: comma separated groups; an empty value removes that capability's entry). Needs `trusted_proxies` or `proxy_secret_header` |

Trust: with neither `trusted_proxies` nor `proxy_secret_header` set, no identity header is trusted, every
request is anonymous, and obs-control logs a warning at startup. With one of them, that check must pass; with both,
**both** must pass. A request that fails the check is anonymous and its identity headers are ignored. A wrong
header name, an unparsable CIDR, an unreadable or too short secret, or `[access.groups]` without trust refuses to
start.

`access.enforce` and `access.allowed_identities` (and their `MALRUNAR_OBS_ACCESS_*` variables) were removed; a
configuration that still sets them fails to load with `unknown setting(s)`.

#### `[storage]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `recordings_dir` | `"/config/recordings"` | `MALRUNAR_OBS_STORAGE_RECORDINGS_DIR`, `MALRUNAR_OBS_RECORDINGS_DIR` | Clips and b-roll |
| `state_dir` | `"/config/state"` | `MALRUNAR_OBS_STORAGE_STATE_DIR`, `MALRUNAR_OBS_STATE_DIR` | Controller state (the selected video profile) |

#### `[relay]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `control_url` | `"http://127.0.0.1:18097"` | `MALRUNAR_OBS_RELAY_CONTROL_URL` | Where the controller reaches the relay's control API |
| `bind_host` | `"127.0.0.1"` | `MALRUNAR_OBS_RELAY_BIND_HOST` | Relay: address the OBS stream arrives on |
| `port` | `18096` | `MALRUNAR_OBS_RELAY_PORT` | Relay: UDP port the OBS stream arrives on |
| `target_host` | (empty) | `MALRUNAR_OBS_RELAY_TARGET_HOST` | Relay: where the stream is forwarded (host); required to run the relay |
| `target_port` | `18096` | `MALRUNAR_OBS_RELAY_TARGET_PORT` | Relay: where the stream is forwarded (port) |
| `control_host` | (empty) | `MALRUNAR_OBS_RELAY_CONTROL_HOST` | Relay: control API address; empty: `bind_host` |
| `control_port` | `18097` | `MALRUNAR_OBS_RELAY_CONTROL_PORT` | Relay: control API port |
| `capture_dir` | `"/tmp/malrunar-obs-captures"` | `MALRUNAR_OBS_RELAY_CAPTURE_DIR` | Relay: where captures are written |

#### `[console]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `enabled` | `true` | `MALRUNAR_OBS_CONSOLE_ENABLED` | Serve the console page at `/` |
| `title` | `"OBS control"` | `MALRUNAR_OBS_CONSOLE_TITLE` | Console title |
| `sender_url` | (empty) | `MALRUNAR_OBS_CONSOLE_SENDER_URL` | A page to embed in the console (a sender, a dashboard); empty: none |
| `secure_url` | (empty) | `MALRUNAR_OBS_CONSOLE_SECURE_URL` | An https URL the console redirects to when opened over http; empty: no redirect |

#### `[plugins]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `modules` | `[]` | `MALRUNAR_OBS_PLUGINS_MODULES`, `MALRUNAR_OBS_PLUGINS` | Plugin modules or `.py` paths |
| `dir` | (empty) | `MALRUNAR_OBS_PLUGINS_DIR`, `MALRUNAR_OBS_PLUGINS_DIR` | Directory whose `*.py` files are loaded as plugins |

#### `[[cameras]]` (an array of tables; the first entry is also set with `MALRUNAR_OBS_CAMERA_<KEY>`)

| Key | Default | Environment variable (first camera) | Meaning |
|---|---|---|---|
| `key` | `"live"` | `MALRUNAR_OBS_CAMERA_KEY` | Scene key in `POST /api/scene/<key>`; the first camera's key is the live scene |
| `scene` | `"Live Feed"` | `MALRUNAR_OBS_CAMERA_SCENE` | Scene name |
| `source` | `"Camera Receiver"` | `MALRUNAR_OBS_CAMERA_SOURCE` | Browser source name |
| `url` | (empty) | `MALRUNAR_OBS_CAMERA_URL` | Page the browser source shows: any web camera page (`file://` paths work too); see the VDO.Ninja example below. Required unless `url_file` is set |
| `url_file` | (empty) | `MALRUNAR_OBS_CAMERA_URL_FILE` | File holding the URL; re-read on every use (keeps secrets out of the config) |
| `width` | `0` | `MALRUNAR_OBS_CAMERA_WIDTH` | Source width; 0 = canvas |
| `height` | `0` | `MALRUNAR_OBS_CAMERA_HEIGHT` | Source height; 0 = canvas |
| `fps` | `0` | `MALRUNAR_OBS_CAMERA_FPS` | Source frame rate; 0 = `video.fps` |
| `css` | `"body { margin: 0; background: #000; overflow: hidden; }"` | `MALRUNAR_OBS_CAMERA_CSS` | Custom CSS injected into the page |
| `restart_when_active` | (unset) | `MALRUNAR_OBS_CAMERA_RESTART_WHEN_ACTIVE` | Reload the page whenever its scene goes on air. Unset: `true`, but `false` with `features.program_control` (a reload drops a peer connection on every return to live) |
| `reroute_audio` | `true` | `MALRUNAR_OBS_CAMERA_REROUTE_AUDIO` | Capture the page's audio into OBS |
| `shutdown_when_hidden` | `false` | `MALRUNAR_OBS_CAMERA_SHUTDOWN_WHEN_HIDDEN` | Unload the page while off air (drops a peer connection) |

Example: VDO.Ninja is one way to bring a remote camera in. Give the sender a push link and show the matching view
link in the browser source (one option among any web camera page):

```toml
[[cameras]]
key = "live"
url = "https://vdo.ninja/?view=<stream-id>&cleanoutput&transparent"   # or: room=<room-name> to view a room
```

`view` selects the stream to show, `room` a room, `cleanoutput` hides the interface and `transparent` makes the page
background transparent. Treat a stream id as a secret and keep it in `url_file` if the config is shared.

#### `[profiles.<name>]` (video profiles, used with `features.video_profiles`)

| Key | Default | Meaning |
|---|---|---|
| `width`, `height` | `640`, `360` | Canvas and output size while the profile is active |
| `encoder_kbps` | `1200` | H.264 bitrate |
| `camera_query` | `{}` | Query parameters set on every camera URL while the profile is active (a way to ask a sender for a lower or higher quality) |
| `camera_query_drop` | `[]` | Query parameters removed from the camera URL (the keys of `camera_query` are always replaced) |

The defaults are `360p` (640x360, 1200), `540p` (960x540, 2000) and `720p` (1280x720, 3000).

#### `[plugin.<name>]`

Free-form tables for plugins (`controller.config.plugin["<name>"]`).

A complete annotated file is `obs-control/examples/obs-control.example.toml`; a test keeps it equal to the
defaults documented here.

### What is generated

For the configuration above the controller writes, before OBS starts: `global.ini` and `user.ini`
(first-run wizard off, existing user preferences kept), the profile (`basic.ini`: canvas,
frame rate, the FFmpeg custom output to `output.url`, H.264 `libx264` with `zerolatency` and AAC,
MPEG-TS with repeated headers), the obs-websocket settings (authentication on, a generated
password kept in the OBS config), and the scene collection:

| Scene | Content |
|---|---|
| one per `[[cameras]]` entry | a browser source with the configured URL, scaled to the canvas |
| `media.scene` | an FFmpeg media source looping `media.file` |
| `stopped.scene` | a solid colour |

In program control the stopped scene is first and on air at start; otherwise it is last and the
first camera is on air. Clip scenes (`<clips.scene_prefix> · <name>`) are created by the controller
as clips appear. Plugins can add more.

## Modes and feature switches

The defaults give the **simple mode**: the first camera is on air, a sender heartbeat starts the
automatic b-roll, and the media scene is a fallback you can switch to. `features.program_control`
switches on **program control**, for deployments where an operator (or a program) decides what is
on air and when audio is released:

| | Simple mode (default) | `features.program_control = true` |
|---|---|---|
| On air at start | first camera | the stopped scene, audio muted |
| `POST /api/scene/<camera key>` | switches the scene | goes live: `409` while a plugin holds the program; `409` without a live sender (when `sender_lease`), and the stopped scene is put on air |
| `POST /api/scene/<media key>` | media scene with today's b-roll | 404 (use `/api/daily-fallback/play`) |
| `POST /api/call/live`, `/stop`, `/microphone`, `/clip-audio` | 404 | go live, stop sending (stopped scene, muted), mute the live audio, mute clip audio |
| `POST /api/clip/<id>/play`, `DELETE /api/clips/<id>` | 404 | play a clip (returns to live or to the stopped scene when it ends), delete a clip not on air |
| `POST /api/clip/<id>/scene` | switch to the clip's scene | 404 |
| `GET /api/input-levels` | 404 | peak levels of the live input and the clip on air |
| Sender heartbeat | starts the b-roll capture | only keeps the sender alive; a stale sender puts the stopped scene on air |
| Automatic b-roll | starts by itself | starts with `POST /api/daily-fallback/ensure`, only while the live scene is on air and the identity matches |
| `features.video_profiles` | | adds `POST /api/call/video-profile` and the profile in `/ready` |

`features.sender_lease = false` removes the heartbeat requirement: the sender counts as always
present (and without program control the b-roll capture starts at startup).

## HTTP API and compatibility table

The API is route-compatible with the controller it was extracted from: every path and method below
is served, with the same request and response bodies, unless the table says otherwise. Responses
are JSON unless noted. "Old" is the previous in-product controller; "new" is `obs-control`.
`<key>` is a scene key, `<id>` a clip id (`[a-z0-9][a-z0-9_-]{0,99}`).

A request that needs OBS while OBS is unavailable gets `503 {"ok": false, "error": ...}`
(the old controller answered `500`). `/health` and `/ready` always answer.

| Method and path | Old | New | Change | Migration |
|---|---|---|---|---|
| `GET /` | Product console page | Generic console: output state, scene buttons, preview, recording, clips | Page rewritten; the sender iframe, `https` redirect and title are configuration | Set `console.sender_url` and `console.secure_url`, or set `console.enabled = false` and use the API |
| `GET /health` | `{healthy, obs, scene, output_active, output_timecode}`, 503 when the output is off | same | none | none |
| `GET /ready` | `{ready, obsVersion, rpcVersion}`; with the product mode also `mode`, mute states, video profile(s), program scene | same keys; the extras appear with `features.program_control`; `mode` is `features.mode_label`; `video_profile(s)` need `features.video_profiles`; plugins can add keys | `mode` default is `program-control`. The server answers `503 {"ready": false}` while OBS starts (the old server refused connections) | Set `features.mode_label` to the old value if a client checks it |
| `GET /api/whoami` | not present | `{user, name, email, groups, capabilities}` of the caller as obs-control sees it (nulls and `[]` for an anonymous caller); needs `viewer` | **new** | none |
| `GET /api/scenes` | not present | `{scenes, program, keys, stopped}` | **new** | none |
| `GET /api/program-preview.jpg` | JPEG of the program scene (640x360), `X-OBS-Scene` header | same | none | none |
| `GET /api/program-live.mjpg` | multipart MJPEG, about 8 fps, 320x180 | same | none | none |
| `GET /media/3-minute-video.mp4` | today's b-roll file | same path | Path keeps its old name; 404 text is `Today's b-roll video is not ready` | none |
| `GET /api/clips` | `{clips: [...]}` | same; `scene_name` default is `<clips.scene_prefix> · <name>` | prefix configurable (default `Recorded`, as before) | none |
| `GET /api/input-levels` | product mode only, else 404 | needs `features.program_control`, else 404; the live input is the first camera | flag name | the old mode switch becomes `features.program_control` |
| `POST /api/clips/upload` | multipart `name`, `file` (1 GiB limit) | same | none | none |
| `POST /api/clips/<id>/trim` | `{start_seconds, end_seconds, name}` | same | none | none |
| `GET /clips/<id>.mp4` | the clip file | same | none | none |
| `DELETE /api/clips/<id>` | product mode only; 409 while on air | needs `features.program_control`; same 409 | flag name | as above |
| `GET /api/recording/status` | relay capture status | same, via `relay.control_url` | none | `MALRUNAR_OBS_RELAY_CONTROL_URL` replaces the old variable |
| `POST /api/recording/start` | `{name}` starts a relay capture | same | none | none |
| `POST /api/recording/stop` | stops, remuxes to MP4, creates the clip scene | same | none | none |
| `GET /api/daily-fallback/status` | b-roll state; in the product mode `?user=` had to be one of two hard-coded names | same state machine and fields; the owner is the trusted identity's user (below); 403 when there is none; `url` is `broll.preview_url`, else `api/daily-fallback/preview` in program control, `media/3-minute-video.mp4` otherwise; `{"enabled": false, "state": "disabled"}` when `broll.enabled = false` | identity handling; the product mode reported `api/obs/daily-fallback/preview` as `url` | Set `broll.preview_url` to the old value if a client relies on it; configure identities (`access.trusted_proxies` and/or `access.proxy_secret_header`; per-identity b-roll applies in program control, as before; no list of identities is needed) |
| `GET /api/daily-fallback/preview` | today's b-roll for the owner | same | none | none |
| `POST /api/daily-fallback/ensure` | 202, starts capture; product mode: 409 unless the owner's camera is live | same | `409 {"error": "Automatic b-roll is disabled"}` when disabled | none |
| `POST /api/daily-fallback/cancel` | cancels the owner's capture | same | none | none |
| `POST /api/daily-fallback/play` | plays today's b-roll (409 if not ready or a plugin holds the program) | same | the holder's message replaces the old take-studio text | none |
| `POST /api/scene/<key>` | keys `live` and `video` | any camera key plus the media key (defaults `live`, `video`); semantics per [modes](#modes-and-feature-switches) | more keys; `409` text `Today's b-roll video is not ready yet` | none for the default keys |
| `POST /api/clip/<id>/scene` | simple mode: switch to the clip's scene | same; 404 in program control | none | none |
| `POST /api/clip/<id>/play` | product mode only: play, optional `{"loop": true}`; 422 when the duration cannot be read; 409 when a plugin holds the program | needs `features.program_control`; same | flag name | as above |
| `POST /api/call/live` | product mode: go live (409 without a live sender) | needs `features.program_control`; goes live with the first camera | flag name | as above |
| `POST /api/call/stop` | product mode: stopped scene, muted | needs `features.program_control`; also releases any plugin holding the program first | flag name | as above |
| `POST /api/call/microphone` | `{"muted": bool}` gates the live audio | same; applies to every camera source | flag name | as above |
| `POST /api/call/clip-audio` | `{"muted": bool}` gates the clip audio | same | flag name | as above |
| `POST /api/call/video-profile` | `{"profile": "360p"\|"540p"\|"720p"}`; response had `scale` and `viewer_kbps` | profiles come from `[profiles.*]`; response `{ok, profile, width, height, encoder_kbps, camera_query}`; needs `features.program_control` and `features.video_profiles`; 409 while a plugin holds the program | the viewer-side `scale`/`viewer_kbps` were sender-specific and became the generic `camera_query` | Put `scale` and `videobitrate` (or whatever the sender understands) in `profiles.<name>.camera_query` |
| `POST /api/sender-heartbeat` | `{"healthy": bool, "user": name}`; simple mode starts the b-roll | same; when identities are configured, the sender's user is the trusted identity's user (403 without one in program control) and the body `user` is ignored; with no identity configuration the body `user` is ignored too (no user) | no hard-coded names | none |
| `POST /api/restart-live` | refreshed the product's receiver input | refreshes the first camera's browser source (`?key=<camera key>` for another one); falls back to resetting its URL | optional `key` parameter | none |
| `POST /api/restart-output` | stop (if running), start | same | none | none |
| `POST /api/stop-output` | stop if running | same | none | none |
| `POST /api/start-output` | start if not running (idempotent) | same | none | none |
| `/api/take-studio/*`, `GET /take-studio/takes/<id>.mp4` | product mode only | **built-in plugin** `malrunar_obs.takes`, off by default, under `/api/takes/*` | renamed and made generic: slides are uploaded files instead of an external materials service; takes and slides are per identity; see the [route mapping](#from-the-previous-take-studio-routes) | enable it with `features.takes = true` and follow the mapping |

Error responses keep the old shapes: `{"ok": false, "error": "..."}` with 400 (bad input), 404
(unknown id or a route disabled by a feature flag), 409 (not allowed now), 422 (unreadable clip),
500, 503 (OBS or the relay unavailable).

## Identity and security

Authentication is **external**: a reverse proxy that authenticates users (for example an SSO forward-auth service
such as Authelia, used as the example below) tells obs-control who the caller is in request headers.
obs-control believes those headers only from a request that proves it came from that proxy (see
[`[access]`](#access)), maps the user's groups to a capability, and refuses what the capability does not allow.
Authelia is an example: nothing is hard-coded, the header names, the trusted addresses and the groups are all
configuration.

```toml
[access]
trusted_proxies = ["198.51.100.0/24"]            # the proxy's network
proxy_secret_header = "X-Obs-Proxy-Secret"      # the proxy adds this header after authenticating
proxy_secret_file = "/run/secrets/obs-proxy-secret"

[access.groups]
operator = ["calls-operators"]
viewer = ["calls-viewers"]
```

### Compatibility: what happens with which configuration

| Configuration | Identity headers | Access checks | b-roll and heartbeat user |
|---|---|---|---|
| Nothing set (the default) | not trusted; everyone is anonymous; a warning is logged at startup | **none: nothing is enforced here**, the proxy in front must restrict access | shared b-roll; the heartbeat has no user (as before) |
| `identity_header` only (an older configuration) | not trusted (a header alone proves nothing); everyone is anonymous | none | per-identity b-roll needs an owner, so in program control b-roll routes and the heartbeat answer 403 until trust is configured |
| `trusted_proxies` and/or `proxy_secret_header`, no `[access.groups]` | trusted when the check passes | an authenticated user is an operator; an anonymous request gets 403 (except `/health`, `/ready`) | per identity (program control) |
| as above plus `[access.groups]` | as above | the user's groups must map to a capability; anonymous and unmapped users get 403 | per identity (program control) |

Previously the header named by `identity_header` was believed from any peer; add `trusted_proxies` or a proxy
secret when upgrading. Without trust configured, **obs-control enforces nothing**: anyone who can reach the port can
use every route, so publish it only on a trusted network or behind the proxy.

### Capabilities

`viewer` may watch (preview, status, who am I); `operator` may do everything else, and implies `viewer`. Groups map
to capabilities with `[access.groups]`. With trust configured and no mapping, every authenticated user is an
operator. `GET /health` and `GET /ready` are always open. A route that is not in the table below (for example one
a plugin registered) is refused when access checks are on, until the plugin declares it with
`controller.classify_route("GET", "/path", "viewer")`; a test asserts that every built-in route is classified.

| Capability | Routes |
|---|---|
| open | `GET /health`, `GET /ready` |
| `viewer` | `GET /` (console), `GET /api/whoami`, `GET /api/scenes`, `GET /api/program-preview.jpg`, `GET /api/program-live.mjpg`, `GET /api/input-levels`, `GET /api/recording/status`, `GET /api/daily-fallback/status` |
| `operator` | `GET /api/clips`, `GET /clips/<id>.mp4`, `GET /api/daily-fallback/preview`, `GET /media/3-minute-video.mp4`, `POST /api/clips/upload`, `POST /api/clips/<id>/trim`, `DELETE /api/clips/<id>`, `POST /api/scene/<key>`, `POST /api/clip/<id>/scene`, `POST /api/clip/<id>/play`, `POST /api/call/*` (`live`, `stop`, `microphone`, `clip-audio`, `video-profile`), `POST /api/recording/start`, `POST /api/recording/stop`, `POST /api/daily-fallback/play`, `POST /api/daily-fallback/cancel`, `POST /api/daily-fallback/ensure`, `POST /api/sender-heartbeat`, `POST /api/restart-live`, `POST /api/restart-output`, `POST /api/stop-output`, `POST /api/start-output`; with the takes plugin enabled every `/api/takes/*` route (below) |

A refused request gets `403 {"ok": false, "error": ...}`.

`GET /api/whoami` answers `{"user": "ann", "name": "Ann Example", "email": "ann@example.invalid",
"groups": ["calls-operators"], "capabilities": ["operator", "viewer"]}`. An anonymous caller gets `null` fields,
`[]` groups and, when access checks are off, both capabilities (nothing is enforced); with checks on, anonymous
callers get 403 instead.

The user name keys per-user state: with identities configured and `features.program_control`, b-roll is kept per
user (any user the proxy passes gets its own; a request without an identity gets 403 from the b-roll routes), and
`POST /api/sender-heartbeat` takes the sender's user from the identity, never from the body or a query parameter.
Identity and secret headers are never forwarded to the relay or logged.

### Behind an authenticating proxy

An example with Authelia and Traefik (generic names; adapt freely, any forward-auth proxy that sets headers works).
Authelia authenticates, Traefik's `forwardAuth` copies the `Remote-*` headers onto the request, and a second
middleware adds the shared secret **after** the forward-auth step so a client cannot supply it.

```yaml
# Traefik dynamic configuration
http:
  middlewares:
    authelia:
      forwardAuth:
        address: "http://authelia:9091/api/authz/forward-auth"
        trustForwardHeader: true
        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Name, Remote-Email]
    obs-secret:
      headers:
        customRequestHeaders:
          X-Obs-Proxy-Secret: "<the secret in /run/secrets/obs-proxy-secret>"
  routers:
    obs:
      rule: "Host(`obs.example.com`)"
      service: obs
      middlewares: [authelia, obs-secret]      # order matters: the secret is set after forward-auth
  services:
    obs:
      loadBalancer:
        servers: [{ url: "http://obs-control.example.invalid:18095" }]
```

```yaml
# Authelia configuration: who may reach the host at all (groups are the ones used in [access.groups])
access_control:
  default_policy: deny
  rules:
    - domain: obs.example.com
      policy: one_factor
      subject: ["group:calls-operators", "group:calls-viewers"]
```

```toml
# obs-control
[access]
trusted_proxies = ["198.51.100.0/24"]       # Traefik's network
proxy_secret_header = "X-Obs-Proxy-Secret"
proxy_secret_file = "/run/secrets/obs-proxy-secret"

[access.groups]
operator = ["calls-operators"]
viewer = ["calls-viewers"]
```

- Put the secret (at least 16 random characters, kept out of the config) in a file mounted at
  `proxy_secret_file` and in the proxy configuration. To rotate, list the new secret second in the file, change the
  proxy, then remove the old one; both work meanwhile.
- Authelia group names are the strings Authelia sends in `Remote-Groups`; `[access.groups]` maps them to
  `viewer` and `operator`. A user in neither list gets 403.
- Do not publish obs-control's port except to the proxy. The proxy must not let a client set `Remote-*` or the
  secret header itself (Traefik's `authResponseHeaders` replaces them).
- Richer integration (more capabilities, per-take ownership, an audit log of who did what) can be added later by
  reading further configurable headers; the existing settings would not change.

obs-websocket is protected with a password (generated on first start, or `obs.websocket_password`)
and is not published by the image. The relay's control API is unauthenticated and binds to
`127.0.0.1` by default. Plugins and `url_file`/config files are trusted input.

## Plugins

A plugin is a Python module loaded by the controller (and by the config generator). One is built in and
optional: [takes](#takes-built-in-plugin). Point at them with
`plugins.modules` / `MALRUNAR_OBS_PLUGINS` (module names or `.py` paths, comma-separated) and
`plugins.dir` / `MALRUNAR_OBS_PLUGINS_DIR` (every `*.py` in the directory, in name order, files starting
with `_` skipped; the directory is added to `sys.path`). A plugin that fails to load stops the start
with a message naming it. Settings go under `[plugin.<name>]`.

Two optional functions:

```python
def extend_collection(builder, config):
    """Called while the scene collection is generated, before OBS starts."""
    builder.add_source("Slide", "image_source", {"file": ""})           # an OBS input
    builder.add_scene("Slides", ["Slide"])                               # a scene of existing sources
    builder.profile_ini.setdefault("AdvOut", {})["TrackIndex"] = "2"    # extra profile settings
    builder.profile_files["service.json"] = "{}"                        # extra profile files


def register(controller):
    """Called once when the controller is built."""
    from aiohttp import web

    async def hello(request):
        (version,) = await controller.obs(("GetVersion", {}))
        return web.json_response({"obs": version["obsVersion"], "cfg": controller.config.plugin["demo"]})

    controller.app.router.add_get("/api/demo/hello", hello)
    controller.app.on_startup.append(my_startup)                  # aiohttp signals
    controller.extra_live_scenes.add("Slides")                    # keep the live audio unmuted in this scene
    controller.add_program_holder(ProgramHolder(...))             # see below
    controller.add_ready_info(lambda: {"demo": True})             # extra keys in GET /ready
```

`builder` is a `malrunar_obs.scenes.CollectionBuilder` (`add_source`, `add_scene`, `has`, `uuid_of`,
`canvas`, `profile_ini`, `profile_files`, `collection_fields`); `config` is the validated
configuration (`config.cameras`, `config.scene_keys`, `config.plugin["name"]`, ...).
`controller` is a `malrunar_obs.controller.Controller`:

| Member | Use |
|---|---|
| `app` | the `aiohttp.web.Application`; add routes, `on_startup`, `on_cleanup` |
| `config` | the configuration |
| `await obs(*requests)` | obs-websocket requests `(type, data)` in order over one connection; returns the response data list; raises `ObsError` |
| `obs_client` | `ObsClient`: `session()` for a long-lived socket (events), `ask(socket, type, data)` |
| `add_program_holder(ProgramHolder(name, is_held, release, message))` | claim the program (for example while a recording studio is active). While `is_held()` is true, going live, playing clips and changing the video profile are refused with `409 {message}`; stopping calls `await release(neutral)` first |
| `held_by()` / `release_program_holders(neutral)` | query / release the holders |
| `add_ready_info(fn)` | merge `fn()` (sync or async dict) into `GET /ready` |
| `extra_live_scenes` | set of scene names in which a camera source is unmuted besides its own scene |
| `sender_alive()`, `sender_last_seen`, `sender_user` | the sender lease |
| `program_audio_muted`, `clip_audio_muted`, `active_clip_input` | audio gate state; `await set_program_audio_muted(muted, strict=False)` applies it |
| `clip_lock` | serialises clip playback and program changes |
| `video_lock` | serialises output start/stop, canvas and video-profile changes (take it around your own) |
| `clip_task` | the task that ends a playing clip |
| `ensure_stopped_scene()`, `stop_program()`, `stopped_scene`, `live_camera`, `scene_name(key)`, `canvas_size()` | scene helpers |
| `fit_scene_to_canvas(scene, width, height)` | scale a scene's items to fit |
| `recordings_dir`, `state_dir`, `clips.metadata(path)`, `clips.ensure_scene(clip)`, `clean_display_name(text)` | clips |
| `identity(request)` | the caller's identity (trusted header, else `?user=`) |
| `ProgramBusy` | raise from an operation that must not run while a holder has the program (the video-profile route answers 409) |

Plugins run in the controller process: they share its event loop, so they must not block. Porting a
plugin from the previous in-product controller is mostly renaming: its module-level state became
members of `controller` (`_sender_last_seen` to `sender_alive()`, `_clip_playback_lock` to
`clip_lock`, `_video_profile_lock` to `video_lock`, `_active_clip_input` to `active_clip_input`,
`SCENES["live"]` to `scene_name("live")`, the neutral scene to `stopped_scene`,
`ensure_neutral_scene` to `ensure_stopped_scene`, `obs_requests` to `obs`,
`RECORDINGS_DIR` to `recordings_dir`, `clip_metadata`/`ensure_clip_scene` to
`clips.metadata`/`clips.ensure_scene`).

## Takes (built-in plugin)

Takes let someone prepare and record videos ahead of time and play them into the program later, so they go out
through the same output (and the virtual camera and microphone behind it) as the live camera. It is an optional
plugin that ships in the image and is **off by default**: nothing below exists (no routes, no scene, no profile
settings) until it is enabled with `features.takes = true` (`MALRUNAR_OBS_FEATURES_TAKES=true`) or by listing
`malrunar_obs.takes` in `plugins.modules`.

The workflow:

1. **Upload slides** (optional): `POST /api/takes/slides` with an image (PNG or JPEG) or a PDF. A PDF is rendered
   to one picture per page. Every upload is a *deck* with an id; a slide is `{"deck_id", "page"}`.
2. **Arm**: `POST /api/takes/arm` puts the take scene on the program and claims it (a [program holder](#plugins):
   going live, clip playback and video-profile changes answer `409` meanwhile; `POST /api/call/stop` always wins).
   The OBS canvas switches to the take size (1920x1080 by default); the program output keeps its configured size
   (it is rescaled), so the receiver does not notice. The scene holds a slide and the take camera in one of three
   layouts: `camera`, `pip` (camera in a corner over the slide) or `slide` (the camera stays on for its sound but is
   moved out of view).
3. **Record**: `POST /api/takes/start`, then `POST /api/takes/stop`. OBS's stream output sends the take scene to a
   local RTMP listener (ffmpeg, `127.0.0.1` only): a separate, full-size encode carrying audio track 2, which only
   the take camera feeds, so the program never carries the take's sound while you prepare. The result is an MP4 in
   your library (`GET /api/takes/library`). Recording is refused while the take camera picture is black (set
   `check_camera = false` for a source that really is).
4. **Assemble**: `POST /api/takes/assemble` trims takes and joins them with stills of slides into one 1080p MP4 in
   the background (`GET /api/takes/jobs/<job_id>`). The result is an ordinary clip (`GET /api/clips`, prefix
   `take-`, `"source": "takes"`).
5. **Play**: leave take mode (`POST /api/takes/disarm`: the previous scene and canvas come back, a running take is
   kept) and put the clip on the program with the existing clip routes: `POST /api/clip/<id>/play` in program
   control, `POST /api/clip/<id>/scene` otherwise.

```toml
[features]
takes = true

[plugin.takes]
camera_url = "https://camera.example.invalid/view?id=take"   # optional: defaults to the first camera's URL
```

### Settings: `[plugin.takes]`

Every key is optional. There are no environment variables for these (plugin tables are file-only); the on switch
is `features.takes`. Unknown keys and wrong types stop the start with a message naming the setting.

| Key | Default | Meaning |
|---|---|---|
| `scene` | `"Take Studio"` | Name of the take scene (must be unused) |
| `slide_source` | `"Take Slide"` | Name of its image source |
| `camera_source` | `"Take Camera"` | Name of its browser source (audio track 2 only; it loads only while the take scene is on air) |
| `camera_url` | (empty) | Page the take camera shows; empty: the first `[[cameras]]` URL. Use a separate stream or page if the live camera allows a single viewer |
| `camera_url_file` | (empty) | File holding that URL, read when the config is generated (not with `camera_url`) |
| `width` | `1920` | Take canvas and recording width (even) |
| `height` | `1080` | Take canvas and recording height (even) |
| `max_take_seconds` | `1800` | A take is stopped and kept after this long |
| `max_upload_mb` | `25` | Largest slide upload (`413` above it) |
| `max_pdf_pages` | `100` | Most pages of an uploaded PDF |
| `rtmp_port` | `1935` | Port of the local RTMP listener used while recording (loopback only) |
| `data_dir` | (empty) | Where takes and slides are kept; empty: `<storage.recordings_dir>/takes` |
| `check_camera` | `true` | Refuse to record while the take camera picture is black |

Generating the configuration also sets, in the OBS profile, the stream output (x264, CRF 20, audio track 2, a
local RTMP service) and, unless `output.rescale` is set, the program output's size to `video.width` x
`video.height`. The image needs `ffmpeg` and, for PDFs, `pdftoppm` and `pdfinfo` (`poppler-utils`, part of the
image).

### Routes

All of them need the `operator` capability and are classified that way (`GET` too: a take is private work).
Errors use the usual `{"ok": false, "error": ...}` shape. `<deck_id>` is `deck-` and 12 hex digits, `<take_id>` is
3-64 characters of `a-z`, `0-9` and `-`, a page is 1-999.

| Method and path | Request | Response and notes |
|---|---|---|
| `GET /api/takes/status` | | `{armed, mine, canvas, layout, slide, recording, jobs}`. When another user holds take mode: `armed: true, mine: false` and nothing else of theirs |
| `GET /api/takes/library` | | `{takes: [...], slides: [...]}` of the caller |
| `POST /api/takes/arm` | `{"layout": "camera"\|"pip"\|"slide", "slide": {"deck_id", "page"} \| null}` (both optional; default `pip`) | Enter take mode (or change layout and slide when you are in it). `404` unknown slide, `409` another user holds it, `503` OBS could not switch (take mode is left again) |
| `POST /api/takes/disarm` | | Stop a running take (kept), restore the previous scene and canvas. The previous scene is restored only if it is safe: in program control a live camera with a live sender, otherwise the stopped scene (muted) |
| `POST /api/takes/layout` | `{"layout": ...}` | `409` when not armed |
| `POST /api/takes/slide` | `{"slide": {"deck_id", "page"} \| null}` | Put a slide on the scene; no waiting for other take operations (cue-friendly) |
| `POST /api/takes/slides` | multipart `name` (optional), `file` | Upload an image or PDF: `201 {"slides": {id, name, kind, pages, extension, created_at}}`. The type is decided by the file's signature (PNG, JPEG, PDF), never by its name; `415` other types, `413` over `max_upload_mb`, `400` unreadable or too long a PDF, `503` without poppler |
| `GET /api/takes/slides/<deck_id>/<page>` | | The page picture |
| `DELETE /api/takes/slides/<deck_id>` | | `409` while one of its pages is on the take scene |
| `POST /api/takes/start` | `{"take_id": "..."}` (optional; generated as `take-<12 hex>`) | Start recording: `409` when not armed, already recording, a render is running, the id exists or the camera picture is black; `503` OBS refused |
| `POST /api/takes/stop` | | `{"ok": true, "take": {take_id, created_at, duration, width, height, has_audio, bytes, auto_stopped}}` |
| `GET /api/takes/<take_id>.mp4` | | The take file |
| `GET /api/takes/<take_id>/audio.mp3` | | On-demand mono 16 kHz MP3 audio derivative; generated with ffmpeg and cached beside the take. Uses the same operator authorization and per-user ownership as the MP4; missing takes return `404`, conversion failures `503` |
| `DELETE /api/takes/<take_id>` | | `409` while it is recording |
| `POST /api/takes/assemble` | `{"name": "...", "items": [...]}`, 1-60 items: `{"type": "take", "take_id", "start": 0, "end": null}` (seconds; `end` omitted: to the end) or `{"type": "still", "deck_id", "page", "seconds": 0.5-120}` | `202 {"job": {job_id, state, progress, total_seconds, clip_id, ...}}`; `400` for any invalid item, `409` while recording or another render runs |
| `GET /api/takes/jobs/<job_id>` | | `state` is `running`, `done` (with `clip`, the same fields as `GET /api/clips`) or `error` |

### Per-user state

When identities are configured (`access.trusted_proxies` and/or `access.proxy_secret_header`, or an
`access.identity_header`) every request needs a user (`403` without one) and slides, takes, recordings and
assembly jobs belong to that user: `<data_dir>/users/<owner>/{slides,takes}`, where `<owner>` is the user name
encoded with the same path-safe function as the b-roll directories (`.` and `..` and path separators can never
reach the file system). Another user cannot list, read, delete or assemble them (`404`). Without identities
everything is shared under `<data_dir>/shared`. There is one take scene and one program, so one user at a time
holds take mode; the others get `409 Take mode is in use by another user`. Assembled clips are ordinary clips in
`storage.recordings_dir` (the clip library is shared in obs-control); their `<id>.json` records the `owner`.

### From the previous take-studio routes

| Old | New | Change |
|---|---|---|
| `GET /api/take-studio/status` | `GET /api/takes/status` | adds `mine`, `canvas`; `slide` is `{deck_id, page}` |
| `POST /api/take-studio/arm` | `POST /api/takes/arm` | `slide.material_id` became `slide.deck_id` |
| `POST /api/take-studio/disarm` | `POST /api/takes/disarm` | none |
| `POST /api/take-studio/layout` | `POST /api/takes/layout` | none |
| `POST /api/take-studio/slide` | `POST /api/takes/slide` | `material_id` became `deck_id` |
| `POST /api/take-studio/slides/missing` | removed | slides are no longer pushed from an external materials service; list yours with `GET /api/takes/library` |
| `PUT /api/take-studio/slides/<material>/<page>` (PNG body) | `POST /api/takes/slides` (multipart image or PDF) | the server creates the deck id and renders PDF pages itself |
| `DELETE /api/take-studio/slides/<material>` | `DELETE /api/takes/slides/<deck_id>` | `404` for an unknown deck |
| `POST /api/take-studio/takes/start` | `POST /api/takes/start` | `take_id` optional |
| `POST /api/take-studio/takes/stop` | `POST /api/takes/stop` | none |
| `GET /take-studio/takes/<id>.mp4` | `GET /api/takes/<take_id>.mp4` | moved under `/api/` |
| `GET /api/take-studio/takes/<id>/audio.mp3` | removed | the transcription helper was product-specific |
| `DELETE /api/take-studio/takes/<id>` | `DELETE /api/takes/<take_id>` | none |
| `POST /api/take-studio/assemble` | `POST /api/takes/assemble` | still items use `deck_id`; `segment_id` was dropped; clip ids start with `take-` instead of `studio-` |
| `GET /api/take-studio/jobs/<id>` | `GET /api/takes/jobs/<job_id>` | none |
| (not present) | `GET /api/takes/library`, `GET /api/takes/slides/<deck_id>/<page>` | **new** |

Old settings: `OBS_TAKE_STUDIO_DIR` is `plugin.takes.data_dir`, `OBS_TAKE_MAX_SECONDS` is
`plugin.takes.max_take_seconds`, `OBS_TAKE_CAMERA_URL` is `plugin.takes.camera_url`. The scene names and the 1080p
size were fixed in code and are settings now (set `scene`, `slide_source` and `camera_source` to keep old names).

### Things to know

- Take mode switches the canvas and restarts the program output for a moment (receivers reconnect), so arm
  before the call, not during it. Entering and leaving take mode are the only times that happens.
- While take mode is on, clip routes that do not check program holders (`POST /api/clip/<id>/scene`, `POST
  /api/scene/<key>` outside program control) can still change the scene: leave take mode before playing a clip.
- Uploaded images are checked by signature and size, not decoded here; OBS and ffmpeg decode them. A PDF is
  rendered by `pdftoppm` with a time limit.
- A controller restart forgets take mode: if the take scene is still on air it is replaced by the stopped scene and
  the canvas is restored; a take being recorded at that moment is lost.

## The UDP relay

`entrypoint.sh relay` (run it as a second container from the same image, usually on the host's
network namespace) forwards the datagrams OBS sends to a target and can capture them to a file on
demand. The controller uses it for manual recordings (`/api/recording/*`) and for automatic b-roll;
without it those routes report the relay as unavailable (`503`) and everything else works. Point
OBS at the relay (`output.url = udp://<relay host>:<relay.port>?pkt_size=1316`) and the relay at the
receiver (`relay.target_host`, `relay.target_port`); the controller reaches the relay's control API at
`relay.control_url`.

| Method and path (relay control API) | Meaning |
|---|---|
| `GET /recording/status` | `{recording, capture_id, started_at, bytes, queued_bytes, duration, error?}` |
| `POST /recording/start` `{"capture_id": "..."}` | start a capture (`409` if one is active) |
| `POST /recording/stop` | stop it and keep the file (`503` if the storage failed) |
| `GET /recording/file/<capture_id>` | the captured MPEG-TS |
| `DELETE /recording/file/<capture_id>` | delete it |

The capture queue is bounded (2 MiB): a slow disk stops the capture, never the forwarding.

## Files, state and restarts

| Path | Content |
|---|---|
| `/config` (the `obs` user's home) | OBS configuration and everything below |
| `/config/.config/obs-studio` | generated profile, scene collection, obs-websocket settings, browser profile |
| `storage.recordings_dir` (`/config/recordings`) | clips (`<id>.mp4` and `<id>.json`), b-roll under `daily-fallback/`, takes and slides under `takes/` (takes plugin) |
| `storage.state_dir` (`/config/state`) | `video-profile.json` |
| `/opt/malrunar/obs-control/lib/malrunar_obs` | the code |
| `/usr/local/share/malrunar/obs-control/examples` | example configuration and test card |

The OBS configuration is regenerated on every start from the configuration, so changing the
configuration and restarting the container is the way to change scenes. Clip scenes are recreated
from `recordings_dir`.

`MALRUNAR_OBS_RESTORE` is for restarting only the controller process while OBS keeps running (in
program control): a JSON object such as `{"program_audio_muted": false, "clip_audio_muted": false,
"active_clip_input": null, "sender_healthy": true, "user": "ann"}` is taken as the controller's state
instead of resetting to the stopped scene.

## Migrating from the previous in-product controller

Environment variables:

| Old | New |
|---|---|
| `OBS_CONTROL_PORT` | `MALRUNAR_OBS_CONTROL_PORT` |
| `OBS_OUTPUT_URL` | `MALRUNAR_OBS_OUTPUT_URL` |
| the sender-viewer URL (`..._URL`, `..._URL_FILE`) | `MALRUNAR_OBS_CAMERA_URL`, `MALRUNAR_OBS_CAMERA_URL_FILE` |
| the sender page URL for the console | `MALRUNAR_OBS_CONSOLE_SENDER_URL` |
| `OBS_SECURE_CONTROL_URL` | `MALRUNAR_OBS_CONSOLE_SECURE_URL` |
| `OBS_MEDIA_FILE` | `MALRUNAR_OBS_MEDIA_FILE` |
| `OBS_RECORDINGS_DIR` | `MALRUNAR_OBS_RECORDINGS_DIR` |
| `OBS_CONFIG_DIR` | `MALRUNAR_OBS_CONFIG_DIR` (must end in `/obs-studio`; OBS reads it through `XDG_CONFIG_HOME`) |
| `OBS_START_SCENE` (a scene name) | `MALRUNAR_OBS_START_SCENE` (a scene key or name) |
| `OBS_RELAY_CONTROL_URL` | `MALRUNAR_OBS_RELAY_CONTROL_URL` |
| `OBS_RELAY_BIND_HOST`, `_PORT`, `_TARGET_HOST`, `_TARGET_PORT`, `_CONTROL_HOST`, `_CONTROL_PORT` | `MALRUNAR_OBS_RELAY_BIND_HOST`, `..._PORT`, `..._TARGET_HOST`, `..._TARGET_PORT`, `..._CONTROL_HOST`, `..._CONTROL_PORT` |
| `OBS_DAILY_FALLBACK_SECONDS`, `OBS_DAILY_CAPTURE_CHUNK_SECONDS`, `OBS_DAILY_CAPTURE_MAX_SECONDS` | `MALRUNAR_OBS_BROLL_SECONDS`, `MALRUNAR_OBS_BROLL_CHUNK_SECONDS`, `MALRUNAR_OBS_BROLL_MAX_SECONDS` |
| `OBS_DAILY_SILENCE_DB`, `OBS_DAILY_MOTION_THRESHOLD`, `OBS_DAILY_MIN_SEGMENT_SECONDS` | `MALRUNAR_OBS_BROLL_SILENCE_DB`, `MALRUNAR_OBS_BROLL_MOTION_THRESHOLD`, `MALRUNAR_OBS_BROLL_MIN_SEGMENT_SECONDS` |
| the product mode switch | `MALRUNAR_OBS_FEATURES_PROGRAM_CONTROL=true` and `MALRUNAR_OBS_FEATURES_VIDEO_PROFILES=true` |
| the video-profile file variable | `MALRUNAR_OBS_STATE_DIR` (the file is `video-profile.json` in it) |
| the controller-restore variable | `MALRUNAR_OBS_RESTORE` |
| take-studio variables | `[plugin.takes]` settings of the built-in [takes plugin](#takes-built-in-plugin) (`features.takes = true`) |

Names that were fixed in code are configuration now. To keep the old scene and source names on an
existing OBS volume, set them: `cameras[0].scene`, `cameras[0].source`, `media.scene`, `media.source`,
`stopped.scene`, `stopped.source` (the defaults are generic names, and the OBS configuration is
regenerated on every start). The profile and collection names (`obs.profile`, `obs.collection`)
default to `malrunar`. The hard-coded allowlist of two users is gone (authorization is external; see
[Identity and security](#identity-and-security)); the
heartbeat's sender lease is `features.sender_lease` and `sender.lease_seconds`. The OBS image runs as
uid/gid 1000 with home `/config`, like the previous container; obs-websocket now needs a password
(generated and stored in the OBS configuration, nothing to do for the controller).
