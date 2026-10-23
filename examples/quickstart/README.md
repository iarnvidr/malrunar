# Quickstart: a desktop with a virtual microphone, fed by OBS you drive over HTTP

Two containers on one machine, no kernel module and no camera needed:

```
 obs (obs-control)  --MPEG-TS over UDP-->  desktop (desktop-base + media-bridge)
  HTTP API on 127.0.0.1:18095                virtual microphone "Virtual Microphone" in PipeWire
                                             (video decoded and discarded; see "Turn on the camera")
```

- `desktop` is `desktop-base` with the [`media-bridge`](../../media-bridge/) component copied in, and PipeWire
  running for the user `desktop` (uid 1000). The bridge receives the stream and creates the microphone.
- `obs` is [`obs-control`](../../obs-control/): headless OBS with a scene collection generated for you. It
  shows a built-in moving test picture with a tone as its "camera", and sends its program to the desktop.

You need Docker with the Compose plugin and about 3 GB of disk for the images.

## Run it

From the root of this repository:

```sh
docker compose -f examples/quickstart/compose.yaml up -d --build
```

The first start pulls the images and builds the small `desktop` image on top of them. OBS takes about a
minute to start; `docker compose -f examples/quickstart/compose.yaml ps` shows both services `healthy`
when it is ready.

## Check that it works

The API is published on `127.0.0.1:18095` only (it has no authentication of its own).

```sh
# 1. OBS is up
curl -fsS http://127.0.0.1:18095/ready              # {"ready": true, "obsVersion": "32.2.0", ...}

# 2. the scenes, and which one is on air
curl -fsS http://127.0.0.1:18095/api/scenes

# 3. switch the program scene (the keys are in "keys" above): "standby" is a title card, "live" the camera page
curl -fsS -X POST http://127.0.0.1:18095/api/scene/standby   # {"ok": true, "scene": "Standby"}
curl -fsS http://127.0.0.1:18095/api/scenes                  # "program": "Standby"
curl -fsS -X POST http://127.0.0.1:18095/api/scene/live      # {"ok": true, "scene": "Live Feed"}

# 4. the virtual microphone exists for the desktop user
docker compose -f examples/quickstart/compose.yaml exec --user desktop desktop pactl list short sources
#   ... malrunar_virtual_mic   PipeWire   s16le 2ch 48000Hz   ...
```

`http://127.0.0.1:18095/` is the built-in console. The full API (preview pictures, clips, recording,
output control, plugins) is in [docs/obs-control-api.md](../../docs/obs-control-api.md).
`curl -fsS http://127.0.0.1:18095/health` reports `"healthy": true` while OBS is sending to the bridge.

To use your own camera page instead of the test picture, set `QUICKSTART_CAMERA_URL`, for example to a
[VDO.Ninja](https://vdo.ninja) view link (see the commented example in `compose.yaml`):

```sh
QUICKSTART_CAMERA_URL='https://vdo.ninja/?view=<stream-id>&cleanoutput' \
  docker compose -f examples/quickstart/compose.yaml up -d
```

Stop and remove everything, including the OBS configuration volume:

```sh
docker compose -f examples/quickstart/compose.yaml down -v
```

## Turn on the camera

Call applications in the desktop see an ordinary camera only when the picture goes to a v4l2loopback
device. A container cannot load kernel modules, so this takes one step on the host (Linux, root;
`apt install v4l2loopback-dkms` and the matching `linux-headers` first):

```sh
sudo media-bridge/host/setup-v4l2loopback.sh --devices 1 --first-nr 10 --label 'Virtual Camera' --persist
```

then start the quickstart with the camera override, which maps `/dev/video10` into the desktop and
switches the bridge's video sink from `null` to `v4l2`:

```sh
docker compose -f examples/quickstart/compose.yaml -f examples/quickstart/compose.camera.yaml up -d --build
```

The bridge gives the `desktop` user access to the device's group by itself. Details of the host script
and the device are in [media-bridge/README.md](../../media-bridge/README.md).

## Pin images in production

`compose.yaml` pins the images by version tag so that the quickstart does not move under you. A tag
can still be re-pushed: in production pin by digest.

```yaml
image: ghcr.io/iarnvidr/obs-control:0.1.0@sha256:<digest>
```

(`docker buildx imagetools inspect ghcr.io/iarnvidr/obs-control:0.1.0` prints the digest.) The three
images are chosen with `MALRUNAR_DESKTOP_BASE_IMAGE`, `MALRUNAR_MEDIA_BRIDGE_IMAGE` and
`MALRUNAR_OBS_CONTROL_IMAGE` (environment or a `.env` file next to where you run `docker compose`); the
defaults are in `compose.yaml` and `desktop/Dockerfile`.

## Files

| File | |
|---|---|
| `compose.yaml` | the `desktop` and `obs` services |
| `compose.camera.yaml` | opt-in: map `/dev/video10` and use it as the video sink |
| `desktop/` | the desktop image: `Dockerfile`, `supervisord.conf` (PipeWire/WirePlumber/pipewire-pulse, then the bridge), helper scripts |
| `test/run.sh` | the steps above as a script; run by `.github/workflows/quickstart.yml` on every pull request |

## Troubleshooting

- `docker compose ... logs desktop` shows PipeWire and the bridge; `logs obs` shows OBS.
- `desktop` stays unhealthy: the microphone is created by the bridge once PipeWire answers; look for
  `virtual microphone ... ready` in its log.
- With the camera override, `malrunar-media-bridge: video device /dev/video10 does not exist` means the
  host step above has not been done.

The two scenes come from a small configuration that `compose.yaml` embeds (the `configs` section, no host
file needed; this needs Docker Compose 2.23.1 or newer). It adds the `standby` scene to the default `live`
one; copy that block to add your own scenes. Every setting is described in
[docs/obs-control-api.md](../../docs/obs-control-api.md).
