# Operating a desktop-base container: restarts and GPU rendering

`desktop-base` ships no xrdp configuration (see its Dockerfile); the consumer image owns it. Two
operational details come up in practice, and each has a tested example in
[`examples/desktop-operations/`](../examples/desktop-operations/).

## 1. Restarting a container keeps its /tmp

`docker stop` followed by `docker start` (or a host reboot) starts the same container again with its old
filesystem. The previous desktop's `/tmp/.X10-lock` and `/tmp/.X11-unix/X10` are still there, so xrdp
starts the new X server on the next free display (`:11`, `:12`, ...). Anything wired to `:10` (session
tools, health checks, screen capture, `DISPLAY=:10`) then reports that there is no desktop.

Either recreate the container (a fresh `/tmp`), or remove the stale files before `xrdp-sesman` starts:
[`clean-stale-x-locks.sh`](../examples/desktop-operations/clean-stale-x-locks.sh). It only touches the xrdp
range `:10`-`:19`, so a separate headless X server such as `Xvfb :99` is unaffected. With supervisord, run
it as a program that starts before the session manager and stays running:

```ini
[program:x-stale-locks]
command=/bin/sh -c '/usr/local/bin/clean-stale-x-locks.sh; exec sleep infinity'
user=root
priority=4          ; lower than sesman
autorestart=false
```

Never run it while a desktop session is up.

## 2. GPU rendering through a render node

On a VM whose host GPU is exposed through virtio-gpu with virgl, Mesa can render on the GPU instead of the
CPU (`llvmpipe`). Three things have to line up:

1. **The render node reaches the container.** Map `/dev/dri/renderD128` only (not `card0`):
   `--device /dev/dri/renderD128` (compose: `devices: ["/dev/dri/renderD128:/dev/dri/renderD128"]`).
2. **xorgxrdp trusts the driver.** Its `DRMAllowList` defaults to `i915 radeon`; add `virtio_gpu`
   ([`xorgxrdp-gpu.conf`](../examples/desktop-operations/xorgxrdp-gpu.conf)).
3. **The application renders through that X server.** Applications on the xrdp desktop (Firefox, the
   window compositor, Thunderbird, ...) get DRI3 and use the node. A software X server such as `Xvfb` has no
   DRI3, so anything running on it (an OBS instance started on `Xvfb`, for example) stays on `llvmpipe`.

Verify in the Xorg log of the desktop session (`~/.xorgxrdp.<display>.log`):

```
rdpPreInit: name [virtio_gpu]
glamor X acceleration enabled on virgl (Mesa ... Graphics ...)
rdpScreenInit: rdpDri3Init ok
```

and check which processes hold the node (`/proc/<pid>/fd/*` pointing at `/dev/dri/renderD128`; read it as the
process owner, because a container's root without `CAP_SYS_PTRACE` cannot see other users' descriptors).
Mesa's own check, with the node mapped, is `eglinfo -B`: the `Surfaceless` and `Device` platforms should name
`virgl (...)`, and `LIBGL_ALWAYS_SOFTWARE=1` should name `llvmpipe`.

In one measurement on such a VM (Intel iGPU host, 1920x1080 `glmark2`) the virgl score was 73 against 27 for
`llvmpipe`, and the guest used 0.06 CPU cores against 6.7. The host does the rendering work, so that figure
does not include the host-side cost.
