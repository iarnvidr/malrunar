# SPDX-License-Identifier: Apache-2.0
"""Program-scene previews: a single JPEG, and a shared multipart MJPEG stream."""
from __future__ import annotations

import asyncio
import base64
import time
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

PREVIEW_WIDTH, PREVIEW_HEIGHT = 640, 360


class Preview:
    """OBS renders the program scene into a small JPEG on request.

    Single-frame requests are serialised so several open pages never ask OBS to render at the
    same moment. The MJPEG stream runs one OBS connection for all viewers, and only while someone
    watches. Each frame carries its time; identical frames in a row mean the picture is not
    changing (a frozen camera), which a page can flag.
    """

    FPS = 8
    WIDTH, HEIGHT = 320, 180

    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        self.lock = asyncio.Lock()
        self.frame: bytes | None = None
        self.scene = ""
        self.seq = 0
        self.viewers = 0
        self.changed = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def program_preview(self, _: web.Request) -> web.Response:
        async with self.lock:
            (scene,) = await self.ctl.obs(("GetCurrentProgramScene", {}))
            scene_name = scene["currentProgramSceneName"]
            (shot,) = await self.ctl.obs(
                (
                    "GetSourceScreenshot",
                    {
                        "sourceName": scene_name,
                        "imageFormat": "jpg",
                        "imageWidth": PREVIEW_WIDTH,
                        "imageHeight": PREVIEW_HEIGHT,
                        "imageCompressionQuality": 55,
                    },
                )
            )
        data = shot.get("imageData", "")
        if "," not in data:
            raise web.HTTPServiceUnavailable(text="OBS did not return a preview image")
        try:
            body = base64.b64decode(data.split(",", 1)[1], validate=True)
        except (ValueError, TypeError) as exc:
            raise web.HTTPServiceUnavailable(text="OBS returned an invalid preview image") from exc
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0", "X-OBS-Scene": scene_name},
        )

    # ------------------------------------------------------------------ MJPEG
    def watch(self) -> None:
        self.viewers += 1
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._produce())

    def leave(self) -> None:
        self.viewers -= 1

    async def _produce(self) -> None:
        client = self.ctl.obs_client
        while self.viewers > 0:
            try:
                async with client.session() as socket:
                    while self.viewers > 0:
                        started = time.monotonic()
                        scene = (await client.ask(socket, "GetCurrentProgramScene")).get("currentProgramSceneName", "")
                        shot = await client.ask(
                            socket,
                            "GetSourceScreenshot",
                            {
                                "sourceName": scene,
                                "imageFormat": "jpg",
                                "imageWidth": self.WIDTH,
                                "imageHeight": self.HEIGHT,
                                "imageCompressionQuality": 60,
                            },
                        )
                        data = shot.get("imageData", "")
                        if "," in data:
                            async with self.changed:
                                self.frame = base64.b64decode(data.split(",", 1)[1])
                                self.scene, self.seq = scene, self.seq + 1
                                self.changed.notify_all()
                        await asyncio.sleep(max(0.0, 1 / self.FPS - (time.monotonic() - started)))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    async def program_live(self, request: web.Request) -> web.StreamResponse:
        """multipart/x-mixed-replace stream of the program scene (about 8 fps, 320x180) while the client stays."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store, max-age=0",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        self.watch()
        seen = 0  # frames are numbered from 1; wait for the first one (or the newest)
        try:
            while True:
                async with self.changed:
                    await asyncio.wait_for(self.changed.wait_for(lambda: self.seq != seen), timeout=10)
                    frame, scene, seen = self.frame, self.scene, self.seq
                head = (
                    f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n"
                    f"X-Frame-Time: {time.time():.3f}\r\nX-OBS-Scene: {quote(scene)}\r\n\r\n"
                ).encode()
                await response.write(head + frame + b"\r\n")
        except (asyncio.TimeoutError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.leave()
        return response

    async def shutdown(self) -> None:
        self.viewers = 0
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
