# media-bridge

A virtual camera and a virtual microphone for any call application running in a remote desktop,
fed by a video stream you control (typically OBS). The bridge receives MPEG-TS over UDP, writes the
video to a v4l2loopback camera and the audio to a PipeWire microphone. Browsers and call apps see an
ordinary camera and microphone.

```
 OBS (or any ffmpeg/MPEG-TS sender)
        | MPEG-TS over UDP
        v
 malrunar-media-bridge (ffmpeg) --video--> /dev/videoN  (v4l2loopback, created on the host)
                                \--audio--> PipeWire source "Virtual Microphone" (pipe-source)
```

The component image is `FROM scratch` and contains only `/opt/malrunar/media-bridge`. ffmpeg,
`pactl`, `pw-metadata` and python3 come from [`desktop-base`](../desktop-base/), so build your
desktop on that and copy the component in:

```dockerfile
FROM ghcr.io/iarnvidr/desktop-base:153.5.1esr
COPY --link --from=ghcr.io/iarnvidr/media-bridge:0.1.0 / /
```

## Contents

| Path (under `/opt/malrunar/media-bridge`) | What it is |
|---|---|
| `bin/malrunar-media-bridge` | The receiver. Configured by environment, see below |
| `bin/malrunar-bridge-logfilter.sh` | Log filter used by the bridge; hides expected decoder noise at startup |
| `bin/malrunar-xrdp-audio-tune` | xrdp audio settings and default speaker/microphone, for xrdp sessions |
| `bin/malrunar-media-profile` | Prints a Chromium or Firefox policy that lets only given sites use the camera and microphone |
| `host/setup-v4l2loopback.sh` | Creates the camera devices **on the host** |
| `share/supervisord-media-bridge.conf.example` | supervisord program |
| `share/malrunar-media-bridge.service.example` | systemd user unit |

Nothing is added to `PATH`; call the scripts by their path.

## The camera device belongs to the host

`/dev/videoN` is created by the `v4l2loopback` kernel module. A container cannot load kernel modules
and does not need to: load the module on the host (VM, workstation or node), then pass the device in.

```sh
# on the host (needs root; apt install v4l2loopback-dkms and the matching linux-headers first)
sudo host/setup-v4l2loopback.sh --devices 2 --first-nr 10 --label 'Camera A' --label 'Camera B' --persist
# in the container
docker run --device /dev/video10 --group-add "$(stat -c %g /dev/video10)" ... 
```

`setup-v4l2loopback.sh` sets `exclusive_caps=1` (needed for browsers to list the device as a camera),
is idempotent, reloads the module only when the layout differs and nothing has a camera open, and
refuses otherwise. `--persist` keeps the setup across reboots; `--dry-run` shows what it would do.
Give each desktop its own device when several run on one host. Inside the container the device
keeps its number, so with `docker run --device /dev/video10:/dev/video10` the default
`MALRUNAR_BRIDGE_VIDEO_DEVICE=/dev/video10` works unchanged.

The container user must be able to write to the device (a group, see `--group-add` above). When the
device is missing or not writable, the bridge says so, explains the host step and exits with status 3.

### No camera device: the null and file sinks

Where the module cannot be loaded (some hosted CI runners, locked-down hosts), pick another video
sink with `MALRUNAR_BRIDGE_VIDEO_SINK`. Neither is a camera: applications cannot use them.

- `null`: the same ffmpeg graph runs (decode, scale, frame-rate conversion) and the frames are
  discarded (`-f null -`). It proves that the stream arrives and decodes, stores nothing and needs no
  disk. Use it when only the microphone or the stream itself matters.
- `file:/some/path`: the scaled frames are also written as raw `yuv420p` (about 40 MB/s at
  1280x720, 30 fps), so the picture can be inspected. The output is **bounded**: it is kept in a
  ring of two files, `/some/path` (being written) and `/some/path.1` (the previous, complete
  segment). A segment is `MALRUNAR_BRIDGE_FILE_MAX_BYTES` (default 64 MiB), rounded down to a whole
  number of frames (but never less than one frame); when `/some/path` reaches that size it becomes
  `/some/path.1`, replacing the older one, and writing starts again in a new `/some/path`. Disk use
  never exceeds twice `MALRUNAR_BRIDGE_FILE_MAX_BYTES`, however long the bridge runs, and every
  file starts on a frame boundary. At 1280x720 the default holds the last 1.5 to 3 seconds of
  video. Restarts of ffmpeg do not reset the ring: the previous `path` is moved to `path.1`
  first, and an empty `path` is simply overwritten. Both files are left behind when the bridge stops.

## Quiet startup

When the bridge joins an MPEG-TS stream in the middle of a GOP, ffmpeg's decoder complains until the
first keyframe arrives (`non-existing PPS 0 referenced`, `Error submitting packet to decoder`,
`no frame!`, `decode_slice_header error`, and similar). That is expected, so for the first
`MALRUNAR_BRIDGE_STARTUP_QUIET_SECONDS` (default 10) of every ffmpeg run, log lines matching those
known decoder messages are dropped by `bin/malrunar-bridge-logfilter.sh`. The quiet period ends early
at ffmpeg's first `frame=N` progress line (visible at `MALRUNAR_BRIDGE_LOGLEVEL=info`). All other
lines, such as `Connection refused` or `Unknown encoder`, always appear, and after the quiet period
everything does. Set it to `0` to see every message.

## Configuration

All settings are environment variables and all are optional.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_BRIDGE_UDP_PORT` | `5004` | UDP port the MPEG-TS stream is received on |
| `MALRUNAR_BRIDGE_BIND` | `0.0.0.0` | Address to listen on; use `127.0.0.1` when the sender is in the same container |
| `MALRUNAR_BRIDGE_VIDEO_DEVICE` | `/dev/video10` | v4l2loopback device written to |
| `MALRUNAR_BRIDGE_VIDEO_SINK` | `v4l2` | `v4l2` (the device above), `null` (decode and discard, stores nothing) or `file:/path` (raw yuv420p frames in a bounded two-file ring, tests) |
| `MALRUNAR_BRIDGE_FILE_MAX_BYTES` | `67108864` (64 MiB) | File sink only: size of one segment, a positive whole number (`0` is rejected). Disk use stays at or below twice this |
| `MALRUNAR_BRIDGE_STARTUP_QUIET_SECONDS` | `10` | Seconds after each ffmpeg start during which expected decoder noise is hidden; `0` shows everything |
| `MALRUNAR_BRIDGE_SIZE` | `1280x720` | Output picture size `WxH` (even numbers); the input is scaled to it |
| `MALRUNAR_BRIDGE_FPS` | `30` | Output frame rate |
| `MALRUNAR_BRIDGE_MIC_NAME` | `malrunar_virtual_mic` | Source name of the microphone (letters, digits, `_.-`) |
| `MALRUNAR_BRIDGE_MIC_DESCRIPTION` | `Virtual Microphone` | Name shown to applications |
| `MALRUNAR_BRIDGE_MIC_DEFAULT` | `1` | Make the microphone the default source; `0` leaves the default alone |
| `MALRUNAR_BRIDGE_START_OUTPUT_URL` | empty (off) | If set, an `http(s)` URL that receives a `POST` once ffmpeg is listening, to make a controller start the stream. Must be idempotent. Nothing is called when empty |
| `MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE` | empty (off) | Optional readable regular file of HTTP headers for the start-output POST. Requires `MALRUNAR_BRIDGE_START_OUTPUT_URL`; re-read for each request attempt so credentials can rotate without restarting the bridge |
| `MALRUNAR_BRIDGE_INPUT_TIMEOUT` | `5` | Seconds without input before ffmpeg gives up and is restarted |
| `MALRUNAR_BRIDGE_WAIT` | `60` | Seconds to wait for PipeWire and the video device at start |
| `MALRUNAR_BRIDGE_RESTART` | `1` | `0` exits after the first ffmpeg run instead of restarting |
| `MALRUNAR_BRIDGE_RESTART_DELAY` | `1` | Seconds between runs |
| `MALRUNAR_BRIDGE_LOGLEVEL` | `warning` | ffmpeg `-loglevel` |
| `MALRUNAR_BRIDGE_PIPE_DIR` | `$XDG_RUNTIME_DIR`, else `$TMPDIR`, else `/tmp` | Where the audio pipe is created |

When using `MALRUNAR_BRIDGE_START_OUTPUT_HEADERS_FILE`, store one literal `Header-Name: value` per
line (for example `Authorization: Bearer <token>`). Blank lines and lines beginning with `#` are
ignored. This is not curl config syntax: no continuations or directives are accepted. Header names
must be HTTP token characters and values may not contain control characters. Keep the file outside
source control, restrict its permissions to the bridge user (for example mode `0600`), and mount it
read-only where possible. The bridge sends headers only to the configured start URL; use HTTPS for
credentials on untrusted networks. A malformed or unreadable file causes that request attempt to be
skipped rather than sent without authentication. The file is opened anew on every POST attempt, so
replacing its contents rotates credentials for subsequent attempts.

Exit status: 2 for bad configuration or missing tools, 3 for a missing PipeWire server, video device or
microphone, otherwise ffmpeg's status when `MALRUNAR_BRIDGE_RESTART=0`.

`malrunar-xrdp-audio-tune` reads `MALRUNAR_AUDIO_SINK` (`xrdp-sink`), `MALRUNAR_BRIDGE_MIC_NAME`,
`MALRUNAR_AUDIO_RATE` (`44100`), `MALRUNAR_AUDIO_QUANTUM` (`4096`), `MALRUNAR_AUDIO_MAX_QUANTUM`
(`8192`) and `MALRUNAR_AUDIO_WAIT` (`30` seconds).

## Sending a stream

Any sender of MPEG-TS with one video and one audio stream works. A test source:

```sh
ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -pix_fmt yuv420p -c:a aac \
  -f mpegts 'udp://<desktop address>:5004?pkt_size=1316'
```

The bridge reads from a UDP listener, so it can start before or after the sender and recovers when
the stream stops and resumes. OBS can send the same stream with its custom FFmpeg output (format
`mpegts`, URL as above). [`obs-control`](../obs-control/) drives OBS and provides the
start-output endpoint for `MALRUNAR_BRIDGE_START_OUTPUT_URL`.

## Running it

- supervisord: `share/supervisord-media-bridge.conf.example`
- systemd (user): `share/malrunar-media-bridge.service.example`
- Order: PipeWire, WirePlumber and pipewire-pulse first. The bridge needs `XDG_RUNTIME_DIR` of the
  desktop user so `pactl` finds the server.

Browser access to the devices: `bin/malrunar-media-profile` prints a policy (Chromium managed
policy or Firefox `policies.json` fragment) that allows only the sites you list to use the camera and
microphone. Putting a policy into Firefox makes your image a modified Firefox build under Mozilla's
distribution policy.

## Not included, on purpose

Session and login helpers (xrdp PAM and `startwm` scripts), network policy files, per-application
URL lists and device-quality checks of one specific deployment are not part of this component: they
depend on a user layout, an identity system or a product, which a generic layer must not assume.
Build them in your own image on top of `desktop-base`.

## Licence

Apache-2.0 ([LICENSE](LICENSE)). The component contains scripts only; the software it runs
(ffmpeg, PipeWire, pulseaudio-utils) is part of `desktop-base` under its own licences, and the
v4l2loopback kernel module (GPL-2.0) is installed on the host by you, not shipped here.
