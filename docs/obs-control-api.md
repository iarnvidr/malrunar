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

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `identity_header` | (empty) | `MALRUNAR_OBS_ACCESS_IDENTITY_HEADER` | Header carrying the caller's identity, set by a trusted reverse proxy (for example `X-Forwarded-User`). Empty: identity comes from the `user` query parameter |
| `allowed_identities` | `[]` | `MALRUNAR_OBS_ACCESS_ALLOWED_IDENTITIES` | Identities that may own b-roll. Non-empty (with `features.program_control`): b-roll is kept per identity and a request without an allowed identity gets 400 |
| `enforce` | `false` | `MALRUNAR_OBS_ACCESS_ENFORCE` | Reject every request except `/health` and `/ready` unless the trusted header names an allowed identity (403) |

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
| `url` | (empty) | `MALRUNAR_OBS_CAMERA_URL` | Page the browser source shows (`file://` paths work too). Required unless `url_file` is set |
| `url_file` | (empty) | `MALRUNAR_OBS_CAMERA_URL_FILE` | File holding the URL; re-read on every use (keeps secrets out of the config) |
| `width` | `0` | `MALRUNAR_OBS_CAMERA_WIDTH` | Source width; 0 = canvas |
| `height` | `0` | `MALRUNAR_OBS_CAMERA_HEIGHT` | Source height; 0 = canvas |
| `fps` | `0` | `MALRUNAR_OBS_CAMERA_FPS` | Source frame rate; 0 = `video.fps` |
| `css` | `"body { margin: 0; background: #000; overflow: hidden; }"` | `MALRUNAR_OBS_CAMERA_CSS` | Custom CSS injected into the page |
| `restart_when_active` | (unset) | `MALRUNAR_OBS_CAMERA_RESTART_WHEN_ACTIVE` | Reload the page whenever its scene goes on air. Unset: `true`, but `false` with `features.program_control` (a reload drops a peer connection on every return to live) |
| `reroute_audio` | `true` | `MALRUNAR_OBS_CAMERA_REROUTE_AUDIO` | Capture the page's audio into OBS |
| `shutdown_when_hidden` | `false` | `MALRUNAR_OBS_CAMERA_SHUTDOWN_WHEN_HIDDEN` | Unload the page while off air (drops a peer connection) |

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
| `GET /api/daily-fallback/status` | b-roll state; in the product mode `?user=` had to be one of two hard-coded names | same state machine and fields; the owner comes from `access.*` (below); `url` is `broll.preview_url`, else `api/daily-fallback/preview` in program control, `media/3-minute-video.mp4` otherwise; `{"enabled": false, "state": "disabled"}` when `broll.enabled = false` | identity handling; the product mode reported `api/obs/daily-fallback/preview` as `url` | Set `broll.preview_url` to the old value if a client relies on it; list identities in `access.allowed_identities` (per-identity b-roll applies in program control, as before) |
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
| `POST /api/sender-heartbeat` | `{"healthy": bool, "user": name}`; simple mode starts the b-roll | same; `user` is accepted only if listed in `access.allowed_identities` | no hard-coded names | none |
| `POST /api/restart-live` | refreshed the product's receiver input | refreshes the first camera's browser source (`?key=<camera key>` for another one); falls back to resetting its URL | optional `key` parameter | none |
| `POST /api/restart-output` | stop (if running), start | same | none | none |
| `POST /api/stop-output` | stop if running | same | none | none |
| `POST /api/start-output` | start if not running (idempotent) | same | none | none |
| `/api/take-studio/*`, `GET /take-studio/takes/<id>.mp4` | product mode only | not part of obs-control | moved out: a private plugin can register them | see [Plugins](#plugins) |

Error responses keep the old shapes: `{"ok": false, "error": "..."}` with 400 (bad input), 404
(unknown id or a route disabled by a feature flag), 409 (not allowed now), 422 (unreadable clip),
500, 503 (OBS or the relay unavailable).

## Identity and security

The HTTP API has **no authentication of its own**. Publish its port only to networks you trust, or
put an authenticating reverse proxy in front of it and tell obs-control which header the proxy
sets:

```toml
[access]
identity_header = "X-Forwarded-User"     # set (and any client value overwritten) by the proxy
allowed_identities = ["ann", "bob"]      # who may own b-roll
enforce = true                           # also reject any other request, except /health and /ready (403)
```

- With `allowed_identities` empty (the default), or without `features.program_control`, there are no
  identities: b-roll is shared and the heartbeat's `user` is ignored.
- With identities listed and `identity_header` empty, the identity is the `user` query parameter
  (the old behaviour): that is only a label, not a trust boundary.
- `enforce` requires `identity_header` and `allowed_identities`, and then trusts the header: the proxy
  must strip it from client requests.

obs-websocket is protected with a password (generated on first start, or `obs.websocket_password`)
and is not published by the image. The relay's control API is unauthenticated and binds to
`127.0.0.1` by default. Plugins and `url_file`/config files are trusted input.

## Plugins

A plugin is a Python module loaded by the controller (and by the config generator). Point at them with
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
| `storage.recordings_dir` (`/config/recordings`) | clips (`<id>.mp4` and `<id>.json`), b-roll under `daily-fallback/` |
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
| take-studio variables | belong to the take plugin |

Names that were fixed in code are configuration now. To keep the old scene and source names on an
existing OBS volume, set them: `cameras[0].scene`, `cameras[0].source`, `media.scene`, `media.source`,
`stopped.scene`, `stopped.source` (the defaults are generic names, and the OBS configuration is
regenerated on every start). The profile and collection names (`obs.profile`, `obs.collection`)
default to `malrunar`. The hard-coded allowlist of two users is `access.allowed_identities`; the
heartbeat's sender lease is `features.sender_lease` and `sender.lease_seconds`. The OBS image runs as
uid/gid 1000 with home `/config`, like the previous container; obs-websocket now needs a password
(generated and stored in the OBS configuration, nothing to do for the controller).
