# SPDX-License-Identifier: Apache-2.0
"""Takes: record videos ahead of time from a dedicated scene, assemble them, play them into the program.

A built-in, optional plugin (off by default; enable it with ``features.takes = true`` or by listing
``malrunar_obs.takes`` in ``plugins.modules``). The workflow:

1. *Arm*: take mode claims the program. The OBS canvas is switched to the take size (1920x1080 by
   default) and the take scene goes on air: a slide (an uploaded image, or one page of an uploaded PDF)
   and a camera, laid out as camera only, picture-in-picture or slide only. The program output keeps its
   own size (it is rescaled), so the receiver never sees the canvas change.
2. *Record*: OBS's stream output sends the take scene to a local RTMP listener (ffmpeg), a separate
   full-size encode that records audio track 2, which only the take camera feeds. The program's audio
   (track 1) never carries it.
3. *Assemble*: trim takes and join them with stills of slides into one MP4 that becomes an ordinary clip
   (``/api/clips``).
4. *Play*: the clip goes into the program with the existing clip routes (``POST /api/clip/<id>/play``, or
   ``/scene`` outside program control), so it leaves through the same output as the live camera.

Settings are under ``[plugin.takes]``; see docs/obs-control-api.md. Takes, slides and the assembly jobs
belong to the caller's identity user when identities are configured (shared otherwise).
"""
from __future__ import annotations

import asyncio
import logging
import json
import math
import os
import re
import shutil
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from aiohttp import web

from .broll import owner_dirname
from .clips import clean_display_name
from .config import Config, ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

PLUGIN_NAME = "takes"
MODULE_NAME = "malrunar_obs.takes"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
LAYOUTS = ("camera", "pip", "slide")
MAX_ASSEMBLY_ITEMS = 60
MAX_PAGE = 999
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
PDF_MAGIC = b"%PDF-"
SLIDE_EXTENSIONS = ("png", "jpg")
OPERATOR = "operator"

SETTINGS_DEFAULTS: dict[str, Any] = {
    "scene": "Take Studio",
    "slide_source": "Take Slide",
    "camera_source": "Take Camera",
    "camera_url": "",  # empty: the first camera's URL
    "camera_url_file": "",
    "width": 1920,
    "height": 1080,
    "max_take_seconds": 1800,
    "max_upload_mb": 25,
    "max_pdf_pages": 100,
    "rtmp_port": 1935,
    "data_dir": "",  # empty: <storage.recordings_dir>/takes
    "check_camera": True,
}


@dataclass(frozen=True)
class Settings:
    scene: str
    slide_source: str
    camera_source: str
    camera_url: str
    camera_url_file: str
    width: int
    height: int
    max_take_seconds: int
    max_upload_mb: int
    max_pdf_pages: int
    rtmp_port: int
    data_dir: str
    check_camera: bool

    @property
    def canvas(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def rtmp_base(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/take"


def settings_from(config: Config) -> Settings:
    """The validated ``[plugin.takes]`` table; raises ConfigError naming the setting."""
    given = config.plugin.get(PLUGIN_NAME, {})
    where = f"[plugin.{PLUGIN_NAME}]"
    if not isinstance(given, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(given) - set(SETTINGS_DEFAULTS)
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    values = dict(SETTINGS_DEFAULTS)
    for key, value in given.items():
        like = SETTINGS_DEFAULTS[key]
        ok = (
            isinstance(value, bool)
            if isinstance(like, bool)
            else isinstance(value, int) and not isinstance(value, bool)
            if isinstance(like, int)
            else isinstance(value, str)
        )
        if not ok:
            kind = "true or false" if isinstance(like, bool) else "an integer" if isinstance(like, int) else "a string"
            raise ConfigError(f"{where} {key}: expected {kind}")
        values[key] = value
    for key in ("scene", "slide_source", "camera_source"):
        if not values[key].strip():
            raise ConfigError(f"{where} {key} must not be empty")
    if len({values["scene"], values["slide_source"], values["camera_source"]}) != 3:
        raise ConfigError(f"{where}: scene, slide_source and camera_source must be three different names")
    if values["camera_url"] and values["camera_url_file"]:
        raise ConfigError(f"{where}: set camera_url or camera_url_file, not both")
    for key, low, high in (
        ("width", 160, 7680),
        ("height", 90, 4320),
        ("max_take_seconds", 5, 86400),
        ("max_upload_mb", 1, 1024),
        ("max_pdf_pages", 1, MAX_PAGE),
        ("rtmp_port", 1, 65535),
    ):
        if not low <= values[key] <= high:
            raise ConfigError(f"{where} {key} must be between {low} and {high}")
    if values["width"] % 2 or values["height"] % 2:
        raise ConfigError(f"{where}: width and height must be even (H.264)")
    return Settings(**values)


def camera_url(config: Config, settings: Settings) -> str:
    if settings.camera_url_file:
        return Path(settings.camera_url_file).read_text(encoding="utf-8").strip()
    if settings.camera_url:
        return settings.camera_url
    primary = config.primary_camera
    if primary is None:
        raise ConfigError(f"[plugin.{PLUGIN_NAME}]: set camera_url (there is no [[cameras]] entry to reuse)")
    return config.camera_url(primary)


# ------------------------------------------------------------------------ the scene collection
def extend_collection(builder, config: Config) -> None:
    settings = settings_from(config)
    url = camera_url(config, settings)
    browser = {
        "url": url,
        "width": settings.width,
        "height": settings.height,
        "fps": config.video.fps,
        # Connects only while the take scene is on air, so outside take mode the live sender keeps
        # its single viewer.
        "shutdown": True,
        "restart_when_active": True,
        "reroute_audio": True,
        "is_local_file": False,
        "css": "body { margin: 0; background: #000; overflow: hidden; }",
    }
    if url.startswith("file://"):
        browser.update({"is_local_file": True, "local_file": unquote(urlsplit(url).path), "url": ""})
    builder.add_source(settings.slide_source, "image_source", {"file": "", "unload": False})
    # Audio track 2 only: the take recording hears the camera, the program (track 1) never does.
    builder.add_source(settings.camera_source, "browser_source", browser, mixers=2)
    builder.add_scene(settings.scene, [(settings.slide_source, settings.canvas), (settings.camera_source, settings.canvas)])
    advanced = builder.profile_ini.setdefault("AdvOut", {})
    advanced.update(
        {
            # The stream output is used only for takes: a local RTMP listener, audio track 2.
            "Encoder": "obs_x264",
            "TrackIndex": "2",
            "RescaleFilter": "0",
            "ApplyServiceSettings": "false",
            "Track2Bitrate": "160",
        }
    )
    if not config.output.rescale:
        # The program output keeps its size while the canvas is the take size.
        advanced.update({"FFRescale": "true", "FFRescaleRes": f"{config.video.width}x{config.video.height}"})
    builder.profile_files["streamEncoder.json"] = json.dumps(
        {"rate_control": "CRF", "crf": 20, "preset": "veryfast", "profile": "high", "keyint_sec": 2}
    )
    builder.profile_files["service.json"] = json.dumps(
        {"type": "rtmp_custom", "settings": {"server": settings.rtmp_base, "key": "take", "use_auth": False}}
    )


# ---------------------------------------------------------------------------------- helpers
def _error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _safe_id(value: Any) -> str | None:
    value = value if isinstance(value, str) else ""
    return value if SAFE_ID.fullmatch(value) else None


def _page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if 1 <= page <= MAX_PAGE else None


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def sniff_upload(head: bytes) -> str | None:
    """The kind of an uploaded slide file from its first bytes: ``png``, ``jpg``, ``pdf`` or None."""
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(JPEG_MAGIC):
        return "jpg"
    if head.startswith(PDF_MAGIC):
        return "pdf"
    return None


def bmp_bright_fraction(data: bytes, threshold: int = 32) -> float:
    """Fraction of pixels brighter than ``threshold`` luma in an uncompressed 24 or 32 bit BMP."""
    if data[:2] != b"BM":
        raise ValueError("not a BMP image")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp not in (24, 32) or width <= 0 or height == 0:
        raise ValueError("unsupported BMP")
    step = bpp // 8
    row = (width * step + 3) & ~3
    bright = 0
    for y in range(abs(height)):
        start = offset + y * row
        for x in range(width):
            blue, green, red = data[start + x * step : start + x * step + 3]
            if 0.299 * red + 0.587 * green + 0.114 * blue > threshold:
                bright += 1
    return bright / (width * abs(height))


def layout_transforms(layout: str, canvas: tuple[int, int]) -> dict[str, Any]:
    """Scene-item transforms of the take scene on a canvas of this size."""
    width, height = canvas
    full = {"positionX": 0.0, "positionY": 0.0, "boundsWidth": float(width), "boundsHeight": float(height)}
    if layout == "camera":
        camera, slide_visible = full, False
    elif layout == "slide":
        # The camera stays active (its microphone is the take audio) but off the visible canvas.
        camera = {"positionX": float(width + 16), "positionY": 0.0, "boundsWidth": 16.0, "boundsHeight": 9.0}
        slide_visible = True
    else:
        pip_w, pip_h, margin = width / 4, height / 4, height / 27
        camera = {
            "positionX": width - pip_w - margin,
            "positionY": height - pip_h - margin,
            "boundsWidth": pip_w,
            "boundsHeight": pip_h,
        }
        slide_visible = True
    base = {
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "alignment": 5,
        "boundsAlignment": 0,
        "scaleX": 1.0,
        "scaleY": 1.0,
        "rotation": 0.0,
    }
    return {"camera": {**base, **camera}, "slide": {**base, **full}, "slide_visible": slide_visible}


async def run_tool(*command: str, timeout: float) -> tuple[int, bytes, bytes]:
    """Run a helper program with a time limit; (return code, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, out, err


async def probe(path: Path) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    data = json.loads(output or b"{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


logger = logging.getLogger(__name__)


class TakeModeBusy(Exception):
    """Take mode is held by another user."""


# ------------------------------------------------------------------------------- the plugin
class Takes:
    def __init__(self, ctl: "Controller", settings: Settings):
        self.ctl = ctl
        self.s = settings
        self.lock = asyncio.Lock()
        self.armed = False
        self.armed_by: str | None = None
        self.layout = "pip"
        self.slide: dict[str, Any] | None = None
        self.previous_scene: str | None = None
        self.restore_canvas: dict[str, tuple] | None = None
        self.recording: dict[str, Any] | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.reset_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ owners and paths
    @property
    def data_dir(self) -> Path:
        return Path(self.s.data_dir) if self.s.data_dir else self.ctl.recordings_dir / "takes"

    def user(self, request: web.Request) -> str | None:
        """The owner of takes and slides: the identity's user when identities are configured, else shared."""
        if not self.ctl.config.identity_active:
            return None
        identity = self.ctl.identity(request)
        if not identity:
            raise web.HTTPForbidden(
                text=json.dumps({"ok": False, "error": "An authenticated take owner is required"}),
                content_type="application/json",
            )
        return identity

    def root(self, user: str | None) -> Path:
        return self.data_dir / "users" / owner_dirname(user) if user else self.data_dir / "shared"

    def takes_dir(self, user: str | None) -> Path:
        return self.root(user) / "takes"

    def slides_dir(self, user: str | None) -> Path:
        return self.root(user) / "slides"

    def _contained(self, path: Path) -> Path:
        base = self.data_dir.resolve()
        resolved = path.resolve()
        if base not in resolved.parents:
            raise web.HTTPNotFound()
        return path

    def take_path(self, user: str | None, take_id: str) -> Path:
        return self._contained(self.takes_dir(user) / f"{take_id}.mp4")

    def deck_dir(self, user: str | None, deck_id: str) -> Path:
        return self._contained(self.slides_dir(user) / deck_id)

    def slide_path(self, user: str | None, deck_id: str, page: int) -> Path | None:
        folder = self.deck_dir(user, deck_id)
        for extension in SLIDE_EXTENSIONS:
            candidate = folder / f"{page:04d}.{extension}"
            if candidate.is_file():
                return candidate
        return None

    def parse_slide(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        deck_id, page = _safe_id(value.get("deck_id")), _page(value.get("page"))
        return {"deck_id": deck_id, "page": page} if deck_id and page else None

    # ------------------------------------------------------------------ status
    def check_owner(self, user: str | None) -> None:
        if self.armed and self.armed_by != user:
            raise TakeModeBusy("Take mode is in use by another user")

    def status_payload(self, user: str | None) -> dict[str, Any]:
        mine = self.armed and self.armed_by == user
        payload: dict[str, Any] = {
            "ok": True,
            "armed": self.armed,
            "mine": mine if self.armed else None,
            "canvas": list(self.s.canvas),
            "layout": self.layout if mine else None,
            "slide": self.slide if mine else None,
            "recording": None,
            "jobs": {
                job_id: self._public_job(job)
                for job_id, job in self.jobs.items()
                if job["state"] == "running" and job["_owner"] == user
            },
        }
        if self.recording and self.recording["owner"] == user:
            payload["recording"] = {
                "take_id": self.recording["take_id"],
                "elapsed_seconds": round(time.time() - self.recording["started_at"], 1),
            }
        return payload

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in job.items() if not key.startswith("_")}

    # ------------------------------------------------------------------ OBS helpers
    async def _wait_record(self, active: bool, attempts: int = 40) -> bool:
        for _ in range(attempts):
            (status,) = await self.ctl.obs(("GetRecordStatus", {}))
            if bool(status.get("outputActive")) == active:
                return True
            await asyncio.sleep(0.1)
        return False

    async def _stop_stream_and_wait(self) -> None:
        """Wait until the native take encoder is inactive before touching the canvas."""
        await self.ctl.obs_client.stop_stream_and_wait()

    async def current_canvas(self) -> dict[str, tuple]:
        (video,) = await self.ctl.obs(("GetVideoSettings", {}))
        return {
            "base": (video.get("baseWidth"), video.get("baseHeight")),
            "output": (video.get("outputWidth"), video.get("outputHeight")),
        }

    async def _start_program_output(self) -> bool:
        """(Re)start the program output after a canvas change.

        OBS rebinds its outputs to the new video pipeline only when it starts an output while none is
        active, so the take stream must already be stopped."""
        for _attempt in range(4):
            try:
                await self.ctl.obs(("StartRecord", {}))
            except Exception:
                pass  # already active, or OBS refused this time; checked below
            if await self._wait_record(True, attempts=30):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _set_canvas(self, size: tuple[int, int], output: tuple[int, int] | None = None) -> None:
        """Switch the OBS canvas (the caller holds ``video_lock``). The program output restarts briefly."""
        output = tuple(output or size)
        logger.info("take canvas restore stage=current_canvas target=%s/%s", size, output)
        current = await self.current_canvas()
        if current["base"] == tuple(size) and current["output"] == output:
            return
        # StopRecord's response acknowledges the request, not encoder teardown.
        # The program recording and the take stream both own video encoders.
        await self.ctl.obs_client.stop_record_and_wait()
        try:
            logger.info("take canvas stage=SetVideoSettings")
            await self.ctl.obs(
                (
                    "SetVideoSettings",
                    {"baseWidth": size[0], "baseHeight": size[1], "outputWidth": output[0], "outputHeight": output[1]},
                )
            )
        finally:
            logger.info("take canvas stage=restart_program_output")
            started = await self._start_program_output()
        if not started:
            raise RuntimeError("OBS did not restart the program output after the canvas change")

    async def _item_ids(self) -> dict[str, int]:
        (items,) = await self.ctl.obs(("GetSceneItemList", {"sceneName": self.s.scene}))
        return {item["sourceName"]: item["sceneItemId"] for item in items.get("sceneItems", []) if "sourceName" in item}

    async def apply_layout(self, layout: str) -> None:
        ids = await self._item_ids()
        transforms = layout_transforms(layout, self.s.canvas)
        scene, camera, slide = self.s.scene, ids[self.s.camera_source], ids[self.s.slide_source]
        await self.ctl.obs(
            ("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": camera, "sceneItemTransform": transforms["camera"]}),
            ("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": slide, "sceneItemTransform": transforms["slide"]}),
            ("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": slide, "sceneItemEnabled": transforms["slide_visible"]}),
            ("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": camera, "sceneItemEnabled": True}),
        )

    async def apply_slide(self, user: str | None, slide: dict[str, Any] | None) -> None:
        path = self.slide_path(user, slide["deck_id"], slide["page"]) if slide else None
        await self.ctl.obs(
            (
                "SetInputSettings",
                {"inputName": self.s.slide_source, "inputSettings": {"file": str(path) if path else "", "unload": False}, "overlay": True},
            )
        )

    async def camera_picture_ok(self) -> tuple[bool, str]:
        if not self.s.check_camera:
            return True, ""
        try:
            (shot,) = await self.ctl.obs(
                (
                    "GetSourceScreenshot",
                    {"sourceName": self.s.camera_source, "imageFormat": "bmp", "imageWidth": 64, "imageHeight": 36},
                )
            )
            import base64

            image = base64.b64decode(shot.get("imageData", "").split(",", 1)[-1])
            fraction = bmp_bright_fraction(image)
        except Exception as exc:
            return False, f"Could not check the camera picture: {exc}"
        if fraction < 0.05:
            return False, "The take camera picture has not arrived yet (it is black). Wait a few seconds and try again."
        return True, ""

    # ------------------------------------------------------------------ arm and release
    async def arm(self, request: web.Request) -> web.Response:
        user = self.user(request)
        body = await _json_body(request)
        layout = body.get("layout", self.layout if self.armed_by == user else "pip")
        if layout not in LAYOUTS:
            return _error("Unknown layout", 400)
        slide = self.parse_slide(body.get("slide"))
        if body.get("slide") and slide is None:
            return _error("Invalid slide reference", 400)
        if slide and self.slide_path(user, slide["deck_id"], slide["page"]) is None:
            return _error("Slide is not uploaded", 404)
        ctl = self.ctl
        async with self.lock:
            try:
                self.check_owner(user)
            except TakeModeBusy as exc:
                return _error(str(exc), 409)
            if self.armed:
                self.layout = layout
                await self.apply_layout(layout)
                if "slide" in body:
                    self.slide = slide
                    await self.apply_slide(user, slide)
                return web.json_response(self.status_payload(user))
            async with ctl.clip_lock, ctl.video_lock:
                try:
                    (current,) = await ctl.obs(("GetCurrentProgramScene", {}))
                    restore = await self.current_canvas()
                except Exception as exc:
                    return _error(f"OBS unavailable: {exc}", 503)
                previous = current.get("currentProgramSceneName")
                if ctl.clip_task and not ctl.clip_task.done():
                    ctl.clip_task.cancel()
                ctl.active_clip_input = None
                # Marked before the canvas changes so a queued video-profile change sees take mode and
                # refuses instead of interleaving.
                self.armed, self.armed_by, self.layout, self.slide = True, user, layout, slide
                self.previous_scene, self.restore_canvas = previous, restore
                try:
                    await self._set_canvas(self.s.canvas)
                    await self.apply_slide(user, slide)
                    await self.apply_layout(layout)
                    await ctl.obs(("SetCurrentProgramScene", {"sceneName": self.s.scene}))
                except Exception as exc:
                    self.armed, self.armed_by, self.previous_scene = False, None, None
                    try:
                        await self._restore_program(previous)
                    except Exception:
                        pass
                    return _error(f"Could not prepare the take scene: {exc}", 503)
        return web.json_response(self.status_payload(user))

    async def _restore_program(self, previous: str | None) -> None:
        """Leave take mode (the caller holds ``video_lock``): the previous scene when it is safe, else the
        stopped scene, then the canvas found when take mode started."""
        ctl = self.ctl
        stage = "select_restore_scene"
        logger.info("take restore begin previous=%r", previous)
        try:
            target = None
            if previous and previous != self.s.scene:
                cameras = {camera.scene for camera in ctl.config.cameras}
                if not ctl.program_control or (previous in cameras and ctl.sender_alive()):
                    stage = "GetSceneList"
                    logger.info("take restore stage=%s", stage)
                    (scene_list,) = await ctl.obs(("GetSceneList", {}))
                    if previous in {scene["sceneName"] for scene in scene_list.get("scenes", [])}:
                        target = previous
            if target:
                stage = "SetCurrentProgramScene(previous)"
                logger.info("take restore stage=%s scene=%r", stage, target)
                await ctl.obs(("SetCurrentProgramScene", {"sceneName": target}))
                if ctl.program_control:
                    await ctl.set_program_audio_muted(ctl.program_audio_muted, strict=False)
            else:
                stage = "ensure_stopped_scene"
                logger.info("take restore stage=%s", stage)
                await ctl.ensure_stopped_scene()
                stage = "SetCurrentProgramScene(stopped)"
                logger.info("take restore stage=%s", stage)
                await ctl.obs(("SetCurrentProgramScene", {"sceneName": ctl.stopped_scene}))
                if ctl.program_control:
                    await ctl.set_program_audio_muted(True, strict=False)
        except Exception:
            logger.exception("take restore failed stage=%s; traceback follows", stage)
            raise
        else:
            stage = "restore_canvas"
            logger.info("take restore stage=%s", stage)
            width, height = ctl.canvas_size()
            restore = self.restore_canvas or {"base": (width, height), "output": (width, height)}
            await self._set_canvas(tuple(restore["base"]), tuple(restore["output"]))
            self.restore_canvas = None

    async def release(self, neutral: bool = False) -> dict[str, Any] | None:
        """Stop any take (keeping it), restore the program and the canvas found at arm time."""
        finished = None
        async with self.lock:
            if self.recording:
                finished = await self._finish_recording()
            if self.armed:
                async with self.ctl.video_lock:
                    previous = None if neutral else self.previous_scene
                    await self._restore_program(previous)
                    self.armed, self.armed_by, self.previous_scene = False, None, None
        return finished

    async def disarm(self, request: web.Request) -> web.Response:
        user = self.user(request)
        try:
            self.check_owner(user)
        except TakeModeBusy as exc:
            return _error(str(exc), 409)
        try:
            finished = await self.release()
        except Exception as exc:
            logger.exception("take disarm failed; armed=%s previous=%r restore_canvas=%r", self.armed, self.previous_scene, self.restore_canvas)
            return _error(f"Could not leave take mode cleanly: {exc}", 503)
        return web.json_response({**self.status_payload(user), "finished_take": finished})

    # ------------------------------------------------------------------ layout and slide
    async def get_status(self, request: web.Request) -> web.Response:
        user = self.user(request)
        payload = self.status_payload(user)
        camera_ready, _ = await self.camera_picture_ok()
        payload["camera_ready"] = camera_ready
        return web.json_response(payload)

    async def set_layout(self, request: web.Request) -> web.Response:
        user = self.user(request)
        layout = (await _json_body(request)).get("layout")
        if layout not in LAYOUTS:
            return _error("Unknown layout", 400)
        async with self.lock:
            if not self.armed:
                return _error("Take mode is not active", 409)
            try:
                self.check_owner(user)
            except TakeModeBusy as exc:
                return _error(str(exc), 409)
            await self.apply_layout(layout)
            self.layout = layout
        return web.json_response(self.status_payload(user))

    async def set_slide(self, request: web.Request) -> web.Response:
        user = self.user(request)
        body = await _json_body(request)
        slide = self.parse_slide(body.get("slide"))
        if body.get("slide") is not None and slide is None:
            return _error("Invalid slide reference", 400)
        if slide and self.slide_path(user, slide["deck_id"], slide["page"]) is None:
            return _error("Slide is not uploaded", 404)
        if not self.armed:
            return _error("Take mode is not active", 409)
        try:
            self.check_owner(user)
        except TakeModeBusy as exc:
            return _error(str(exc), 409)
        # Slide changes are latency sensitive (cues): no wait for the take-mode lock.
        await self.apply_slide(user, slide)
        self.slide = slide
        return web.json_response(self.status_payload(user))

    # ------------------------------------------------------------------ slides (uploaded files)
    def deck_meta(self, user: str | None, deck_id: str) -> dict[str, Any] | None:
        try:
            meta = json.loads((self.deck_dir(user, deck_id) / "deck.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def list_decks(self, user: str | None) -> list[dict[str, Any]]:
        base = self.slides_dir(user)
        decks = []
        if base.is_dir():
            for folder in base.iterdir():
                if _safe_id(folder.name) and folder.is_dir():
                    meta = self.deck_meta(user, folder.name)
                    if meta:
                        decks.append(meta)
        return sorted(decks, key=lambda deck: deck.get("created_at", ""), reverse=True)

    async def upload_slides(self, request: web.Request) -> web.Response:
        user = self.user(request)
        limit = self.s.max_upload_bytes
        deck_id = f"deck-{uuid.uuid4().hex[:12]}"
        slides = self.slides_dir(user)
        upload = slides / f".{deck_id}.upload"
        render = slides / f".{deck_id}.render"
        folder = self.deck_dir(user, deck_id)
        name = "Slides"
        kind = None
        if not request.content_type.startswith("multipart/"):
            return _error("Send the file as multipart/form-data (fields: name, file)", 400)
        try:
            slides.mkdir(parents=True, exist_ok=True)
            reader = await request.multipart()
            while field := await reader.next():
                if field.name == "name" and not field.filename:
                    text = await field.read_chunk(size=1024)
                    name = clean_display_name(text.decode("utf-8", "replace")) if text else name
                elif field.name == "file" and field.filename and kind is None:
                    total = 0
                    head = b""
                    with upload.open("xb") as target:
                        while chunk := await field.read_chunk(size=1024 * 1024):
                            if not head:
                                head = chunk[:16]
                                kind = sniff_upload(head)
                                if kind is None:
                                    return _error("Upload a PNG or JPEG image or a PDF", 415)
                            total += len(chunk)
                            if total > limit:
                                return _error(f"The file is larger than {self.s.max_upload_mb} MB", 413)
                            target.write(chunk)
            if kind is None or not upload.is_file():
                return _error("Choose one image or PDF file to upload", 400)
            if kind == "pdf":
                pages = await self._render_pdf(upload, render)
                folder.mkdir()
                for number, path in enumerate(pages, 1):
                    os.replace(path, folder / f"{number:04d}.png")
                extension = "png"
            else:
                folder.mkdir()
                extension = kind
                os.replace(upload, folder / f"0001.{extension}")
                pages = [folder / f"0001.{extension}"]
            meta = {
                "id": deck_id,
                "name": name,
                "kind": "pdf" if kind == "pdf" else "image",
                "pages": len(pages),
                "extension": extension,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (folder / "deck.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return web.json_response({"ok": True, "slides": meta}, status=201)
        except ValueError as exc:
            return _error(str(exc), 400)
        except FileNotFoundError:
            return _error("PDF upload needs pdftoppm and pdfinfo (poppler-utils) in the image", 503)
        except asyncio.TimeoutError:
            return _error("Rendering the PDF took too long", 400)
        finally:
            upload.unlink(missing_ok=True)
            shutil.rmtree(render, ignore_errors=True)
            if folder.is_dir() and not (folder / "deck.json").is_file():
                shutil.rmtree(folder, ignore_errors=True)

    async def _render_pdf(self, source: Path, work: Path) -> list[Path]:
        """Rasterise every page of a PDF to PNG files (work dir); the paths in page order."""
        code, out, _ = await run_tool("pdfinfo", str(source), timeout=30)
        match = re.search(rb"^Pages:\s+(\d+)", out, re.M)
        if code or not match:
            raise ValueError("The PDF could not be read")
        count = int(match.group(1))
        if count < 1:
            raise ValueError("The PDF has no pages")
        if count > self.s.max_pdf_pages:
            raise ValueError(f"The PDF has {count} pages; the limit is {self.s.max_pdf_pages}")
        work.mkdir()
        longest = max(self.s.canvas)
        code, _, _ = await run_tool(
            "pdftoppm", "-png", "-scale-to", str(longest), str(source), str(work / "page"), timeout=180
        )
        pages = sorted(
            (int(found.group(1)), path)
            for path in work.glob("page-*.png")
            if (found := re.fullmatch(r"page-(\d+)\.png", path.name))
        )
        if code or len(pages) != count:
            raise ValueError("The PDF could not be rendered")
        return [path for _, path in pages]

    async def slide_image(self, request: web.Request) -> web.StreamResponse:
        user = self.user(request)
        deck_id, page = _safe_id(request.match_info["deck_id"]), _page(request.match_info["page"])
        path = self.slide_path(user, deck_id, page) if deck_id and page else None
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    async def delete_slides(self, request: web.Request) -> web.Response:
        user = self.user(request)
        deck_id = _safe_id(request.match_info["deck_id"])
        if not deck_id:
            raise web.HTTPNotFound()
        folder = self.deck_dir(user, deck_id)
        if not folder.is_dir():
            raise web.HTTPNotFound()
        if self.armed and self.armed_by == user and self.slide and self.slide["deck_id"] == deck_id:
            return _error("These slides are on the take scene", 409)
        shutil.rmtree(folder, ignore_errors=True)
        return web.json_response({"ok": True, "deleted": deck_id})

    # ------------------------------------------------------------------ recording a take
    def list_takes(self, user: str | None) -> list[dict[str, Any]]:
        base = self.takes_dir(user)
        takes = []
        if base.is_dir():
            for path in base.glob("*.mp4"):
                if not _safe_id(path.stem):
                    continue
                try:
                    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {"take_id": path.stem}
                takes.append({**meta, "take_id": path.stem, "bytes": path.stat().st_size, "url": f"api/takes/{path.stem}.mp4"})
        return sorted(takes, key=lambda take: take.get("created_at", ""), reverse=True)

    async def library(self, request: web.Request) -> web.Response:
        user = self.user(request)
        return web.json_response({"ok": True, "takes": self.list_takes(user), "slides": self.list_decks(user)})

    async def start_take(self, request: web.Request) -> web.Response:
        user = self.user(request)
        body = await _json_body(request)
        take_id = _safe_id(body["take_id"]) if "take_id" in body else f"take-{uuid.uuid4().hex[:12]}"
        if not take_id:
            return _error("The take id must be 3-64 characters: lower case letters, digits and '-'", 400)
        async with self.lock:
            if not self.armed:
                return _error("Enter take mode first", 409)
            try:
                self.check_owner(user)
            except TakeModeBusy as exc:
                return _error(str(exc), 409)
            if self.recording:
                return _error("A take is already recording", 409)
            if any(job["state"] == "running" for job in self.jobs.values()):
                return _error("Wait for the assembly render to finish before recording", 409)
            target = self.take_path(user, take_id)
            if target.exists():
                return _error("Take id already exists", 409)
            ok, reason = await self.camera_picture_ok()
            if not ok:
                return _error(reason, 409)
            target.parent.mkdir(parents=True, exist_ok=True)
            key = f"{take_id}-{uuid.uuid4().hex[:8]}"
            partial = target.parent / f".{take_id}.partial.mkv"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-listen", "1",
                "-i", f"{self.s.rtmp_base}/{key}", "-map", "0", "-c", "copy", "-y", str(partial),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0.7)  # the listener must be up before OBS connects
            try:
                if process.returncode is not None:
                    raise RuntimeError("take listener exited")
                await self.ctl.obs(
                    (
                        "SetStreamServiceSettings",
                        {"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": self.s.rtmp_base, "key": key}},
                    ),
                    ("StartStream", {}),
                )
                for _ in range(50):
                    (stream,) = await self.ctl.obs(("GetStreamStatus", {}))
                    if stream.get("outputActive"):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("OBS did not start the take output")
            except Exception as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                partial.unlink(missing_ok=True)
                return _error(f"Could not start the take: {exc}", 503)
            self.recording = {
                "take_id": take_id, "owner": user, "started_at": time.time(), "process": process, "partial": partial,
            }
            self.recording["watchdog"] = asyncio.create_task(self._watchdog(take_id))
        return web.json_response(self.status_payload(user))

    async def _watchdog(self, take_id: str) -> None:
        await asyncio.sleep(self.s.max_take_seconds)
        async with self.lock:
            if self.recording and self.recording["take_id"] == take_id:
                await self._finish_recording(from_watchdog=True)

    async def _finish_recording(self, from_watchdog: bool = False) -> dict[str, Any]:
        """Stop the stream output and finalize the take. The caller holds ``self.lock``."""
        recording = self.recording
        assert recording is not None
        if not from_watchdog and recording.get("watchdog"):
            recording["watchdog"].cancel()
        take_id, process, partial, user = recording["take_id"], recording["process"], recording["partial"], recording["owner"]
        # Subscribe before issuing the stop: a second stop can miss the event or
        # return OutputNotRunning after the first one already completed.
        await self._stop_stream_and_wait()
        self.recording = None
        try:
            await asyncio.wait_for(process.wait(), timeout=20)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()
        final = self.take_path(user, take_id)
        temporary = final.with_suffix(".partial.mp4")
        remux = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(partial), "-map", "0",
            "-c", "copy", "-movflags", "+faststart", "-y", str(temporary),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await remux.communicate()
        partial.unlink(missing_ok=True)
        if remux.returncode:
            temporary.unlink(missing_ok=True)
            return {
                "take_id": take_id, "ok": False,
                "error": stderr.decode("utf-8", "replace")[-300:] or "Could not finalize the take",
            }
        os.replace(temporary, final)
        info = await probe(final)
        meta = {
            "take_id": take_id, "ok": True, "created_at": datetime.now(timezone.utc).isoformat(),
            "auto_stopped": from_watchdog, **info, "bytes": final.stat().st_size,
        }
        final.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    async def stop_take(self, request: web.Request) -> web.Response:
        user = self.user(request)
        async with self.lock:
            if not self.recording or self.recording["owner"] != user:
                return _error("No take is recording", 409)
            result = await self._finish_recording()
        if not result.get("ok"):
            return _error(result.get("error", "Could not finalize the take"), 500)
        return web.json_response({"ok": True, "take": result})

    def _take_file(self, request: web.Request) -> tuple[str | None, str, Path]:
        user = self.user(request)
        take_id = _safe_id(request.match_info["take_id"])
        if not take_id:
            raise web.HTTPNotFound()
        path = self.take_path(user, take_id)
        if not path.is_file():
            raise web.HTTPNotFound()
        return user, take_id, path

    async def take_media(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self._take_file(request)[2], headers={"Cache-Control": "private, max-age=3600"})

    async def take_audio(self, request: web.Request) -> web.StreamResponse:
        """Generate and cache an audio-only MP3 derivative of a take."""
        _, _, source = self._take_file(request)
        target = source.with_suffix(".mp3")
        if not target.is_file():
            partial = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.partial.mp3")
            try:
                code, _, stderr = await run_tool(
                    "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame",
                    "-q:a", "4", "-y", str(partial), timeout=120,
                )
                if code != 0 or not partial.is_file() or partial.stat().st_size == 0:
                    raise RuntimeError(stderr.decode("utf-8", "replace")[-400:] or "ffmpeg failed")
                os.replace(partial, target)
            except (OSError, asyncio.TimeoutError, RuntimeError) as exc:
                partial.unlink(missing_ok=True)
                return _error(f"Could not generate take audio: {exc}", 503)
        return web.FileResponse(target, headers={"Content-Type": "audio/mpeg", "Cache-Control": "private, max-age=3600"})

    async def delete_take(self, request: web.Request) -> web.Response:
        user, take_id, path = self._take_file(request)
        if self.recording and self.recording["owner"] == user and self.recording["take_id"] == take_id:
            return _error("This take is recording", 409)
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        path.with_suffix(".mp3").unlink(missing_ok=True)
        return web.json_response({"ok": True, "deleted": take_id})

    # ------------------------------------------------------------------ assembling a video
    def _assembly_inputs(self, user: str | None, items: list) -> tuple[list[str], list[tuple[str, str]], float]:
        """ffmpeg input arguments, the (video, audio) stream labels of each item and the total length."""
        args: list[str] = []
        labels: list[tuple[str, str]] = []
        total = 0.0
        index = 0
        for number, item in enumerate(items, 1):
            kind = item.get("type")
            if kind == "take":
                take_id = _safe_id(item.get("take_id"))
                if not take_id or not self.take_path(user, take_id).is_file():
                    raise ValueError(f"Item {number}: take not found")
                start = _number(item.get("start"), 0.0)
                end = _number(item.get("end"))
                duration = item.get("_duration")
                if start < 0 or (end is not None and end <= start):
                    raise ValueError(f"Item {number}: trim out must be after trim in")
                if duration is not None:
                    end = min(end, duration) if end is not None else duration
                    if start >= end:
                        raise ValueError(f"Item {number}: trim is outside the take")
                args += ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end is not None else [])
                args += ["-i", str(self.take_path(user, take_id))]
                video, audio = f"{index}:v:0", f"{index}:a:0"
                if not item.get("_has_audio", True):
                    args += ["-f", "lavfi", "-t", f"{(end or 0) - start:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                    index += 1
                    audio = f"{index}:a:0"
                index += 1
                total += (end - start) if end is not None else 0
            elif kind == "still":
                deck_id, page = _safe_id(item.get("deck_id")), _page(item.get("page"))
                seconds = _number(item.get("seconds"), 0.0)
                image = self.slide_path(user, deck_id, page) if deck_id and page else None
                if image is None:
                    raise ValueError(f"Item {number}: slide image not found")
                if not 0.5 <= seconds <= 120:
                    raise ValueError(f"Item {number}: still duration must be 0.5-120 seconds")
                args += ["-loop", "1", "-framerate", "24", "-t", f"{seconds:.3f}", "-i", str(image)]
                args += ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                video, audio = f"{index}:v:0", f"{index + 1}:a:0"
                index += 2
                total += seconds
            else:
                raise ValueError(f"Item {number}: unknown item type")
            labels.append((video, audio))
        return args, labels, total

    def assembly_command(self, user: str | None, items: list, output: Path) -> tuple[list[str], float]:
        args, labels, total = self._assembly_inputs(user, items)
        width, height = self.s.canvas
        graph = []
        for number, (video, audio) in enumerate(labels):
            graph.append(
                f"[{video}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps=24,format=yuv420p,setsar=1[v{number}]"
            )
            graph.append(f"[{audio}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{number}]")
        graph.append("".join(f"[v{n}][a{n}]" for n in range(len(labels))) + f"concat=n={len(labels)}:v=1:a=1[v][a]")
        command = [
            "nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1",
            *args, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-y", str(output),
        ]
        return command, total

    async def assemble(self, request: web.Request) -> web.Response:
        user = self.user(request)
        body = await _json_body(request)
        try:
            items = body.get("items")
            if not isinstance(items, list) or not items or len(items) > MAX_ASSEMBLY_ITEMS:
                raise ValueError(f"Choose between 1 and {MAX_ASSEMBLY_ITEMS} timeline items")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Invalid timeline item")
                take_id = _safe_id(item.get("take_id")) if item.get("type") == "take" else None
                if take_id and self.take_path(user, take_id).is_file():
                    info = await probe(self.take_path(user, take_id))
                    item["_duration"], item["_has_audio"] = info["duration"], info["has_audio"]
            name = clean_display_name(str(body.get("name") or "Assembled take"))
            clip_id = f"take-{uuid.uuid4().hex[:16]}"
            partial = self.ctl.recordings_dir / f".{clip_id}.partial.mp4"
            command, total = self.assembly_command(user, items, partial)
        except (TypeError, ValueError) as exc:
            return _error(str(exc), 400)
        if self.recording:
            return _error("Finish the take before rendering", 409)
        if any(job["state"] == "running" for job in self.jobs.values()):
            return _error("Another assembly is rendering", 409)
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id, "state": "running", "progress": 0.0, "clip": None, "error": None,
            "clip_id": clip_id, "name": name, "total_seconds": round(total, 2), "_owner": user,
        }
        self.jobs[job_id] = job
        job["_task"] = asyncio.create_task(self._run_assembly(job, command, partial, total, user))
        return web.json_response({"ok": True, "job": self._public_job(job)}, status=202)

    async def _run_assembly(self, job: dict, command: list[str], partial: Path, total: float, user: str | None) -> None:
        ctl = self.ctl
        ctl.recordings_dir.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            while line := await process.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("out_time_us=") and total > 0:
                    try:
                        job["progress"] = min(0.99, int(text.split("=", 1)[1]) / 1e6 / total)
                    except ValueError:
                        pass
            stderr = await process.stderr.read()
            await process.wait()
            if process.returncode:
                raise RuntimeError(stderr.decode("utf-8", "replace")[-400:] or "ffmpeg failed")
            target = ctl.recordings_dir / f"{job['clip_id']}.mp4"
            os.replace(partial, target)
            record = {
                "id": job["clip_id"],
                "display_name": job["name"],
                "scene_name": f"{ctl.config.clips.scene_prefix} · {job['name']} · {job['clip_id'][-8:]}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "takes",
                "owner": user,
            }
            target.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            clip = ctl.clips.metadata(target)
            try:
                await ctl.clips.ensure_scene(clip)
                if not ctl.program_control:
                    await ctl.fit_scene_to_canvas(clip["scene_name"], *ctl.canvas_size())
            except Exception:
                pass  # the scene is created again on first play or on restart
            job.update({"state": "done", "progress": 1.0, "clip": clip})
        except asyncio.CancelledError:
            job.update({"state": "error", "error": "cancelled"})
            raise
        except Exception as exc:
            job.update({"state": "error", "error": str(exc)})
        finally:
            partial.unlink(missing_ok=True)
            job.pop("_task", None)
            for old in [key for key, value in self.jobs.items() if value["state"] != "running"][:-20]:
                self.jobs.pop(old, None)

    async def job_status(self, request: web.Request) -> web.Response:
        user = self.user(request)
        job = self.jobs.get(request.match_info["job_id"])
        if not job or job["_owner"] != user:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True, "job": self._public_job(job)})

    # ------------------------------------------------------------------ lifecycle
    async def reset_on_startup(self, _app: web.Application) -> None:
        """A controller restart forgets take mode: never leave the take scene on air at the take canvas."""

        async def task() -> None:
            for _attempt in range(30):
                try:
                    (scene,) = await self.ctl.obs(("GetCurrentProgramScene", {}))
                    if scene.get("currentProgramSceneName") == self.s.scene:
                        async with self.ctl.video_lock:
                            try:
                                (stream,) = await self.ctl.obs(("GetStreamStatus", {}))
                                if stream.get("outputActive"):
                                    await self._stop_stream_and_wait()
                            except Exception:
                                logger.exception("could not confirm stream STOPPED during startup recovery")
                                return
                            self.restore_canvas = None
                            await self._restore_program(None)
                    return
                except Exception:
                    await asyncio.sleep(2)

        self.reset_task = asyncio.create_task(task())

    async def cleanup(self, _app: web.Application) -> None:
        if self.reset_task is not None and not self.reset_task.done():
            self.reset_task.cancel()
        if self.recording:
            self.recording["watchdog"].cancel()
            process = self.recording["process"]
            if process.returncode is None:
                process.kill()
            self.recording["partial"].unlink(missing_ok=True)
            self.recording = None
        for job in list(self.jobs.values()):
            running = job.get("_task")
            if running is not None and not running.done():
                running.cancel()

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", "/api/takes/status", self.get_status),
            ("GET", "/api/takes/library", self.library),
            ("POST", "/api/takes/arm", self.arm),
            ("POST", "/api/takes/disarm", self.disarm),
            ("POST", "/api/takes/layout", self.set_layout),
            ("POST", "/api/takes/slide", self.set_slide),
            ("POST", "/api/takes/slides", self.upload_slides),
            ("GET", "/api/takes/slides/{deck_id}/{page}", self.slide_image),
            ("DELETE", "/api/takes/slides/{deck_id}", self.delete_slides),
            ("POST", "/api/takes/start", self.start_take),
            ("POST", "/api/takes/stop", self.stop_take),
            ("GET", "/api/takes/{take_id}.mp4", self.take_media),
            ("GET", "/api/takes/{take_id}/audio.mp3", self.take_audio),
            ("DELETE", "/api/takes/{take_id}", self.delete_take),
            ("POST", "/api/takes/assemble", self.assemble),
            ("GET", "/api/takes/jobs/{job_id}", self.job_status),
        ]


def register(controller: "Controller") -> None:
    from .controller import ProgramHolder

    takes = Takes(controller, settings_from(controller.config))
    controller.takes = takes  # type: ignore[attr-defined]
    for method, path, handler in takes.routes():
        controller.app.router.add_route(method, path, handler)
        controller.classify_route(method, path, OPERATOR)
    controller.add_program_holder(
        ProgramHolder(
            "takes",
            lambda: takes.armed,
            takes.release,
            "Leave take mode before using the program",
        )
    )
    controller.add_ready_info(lambda: {"takes": {"armed": takes.armed}})
    controller.app.on_startup.append(takes.reset_on_startup)
    controller.app.on_cleanup.append(takes.cleanup)
