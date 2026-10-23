# SPDX-License-Identifier: Apache-2.0
"""The obs-control HTTP controller: one aiohttp application driving OBS through obs-websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web
from websockets.exceptions import WebSocketException

from . import broll as broll_module
from . import clips as clips_module
from . import console as console_module
from . import levels as levels_module
from . import preview as preview_module
from .config import ENV_RESTORE, Config, Section, load_config
from .obsws import ObsClient, ObsError
from .plugins import call_hook, load_plugins
from .scenes import color_value, websocket_password

log = logging.getLogger("malrunar_obs")
CONTROLLER_KEY = web.AppKey("controller", object)


class ProgramBusy(Exception):
    """Raised by an operation that must not run while a plugin holds the program."""


@dataclass
class ProgramHolder:
    """A plugin that can take over the program output (for example a recording studio).

    ``is_held()`` says whether it holds the program now; ``release(neutral)`` gives it back (the
    controller calls it before switching to the stopped scene); ``message`` is the error text for
    operations refused while it is held.
    """

    name: str
    is_held: Callable[[], bool]
    release: Callable[[bool], Awaitable[Any]] | None = None
    message: str = "Another component is using the program; leave it first"


def local_day() -> str:
    return datetime.now().astimezone().date().isoformat()


def camera_profile_url(url: str, profile: dict[str, Any]) -> str:
    """The camera URL with this profile's ``camera_query`` applied (keys it names are replaced)."""
    extra = profile.get("camera_query") or {}
    drop = set(profile.get("camera_query_drop") or []) | set(extra)
    if not extra and not drop:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop]
    query += [(k, str(v)) for k, v in extra.items()]
    return urlunsplit(parts._replace(query=urlencode(query, safe=":/")))


class Controller:
    def __init__(self, config: Config, plugins: list[Any] | None = None, obs: ObsClient | None = None):
        self.config = config
        self.plugins = plugins or []
        self.obs_client = obs or ObsClient(config.websocket_url, lambda: websocket_password(config))
        self.recordings_dir = Path(config.storage.recordings_dir)
        self.state_dir = Path(config.storage.state_dir)
        # -- state
        self.sender_last_seen = 0.0
        self.sender_user: str | None = None
        self.program_audio_muted = False
        self.clip_audio_muted = False
        self.active_clip_input: str | None = None
        self.video_profile = config.video.default_profile
        self.program_scene_generation = 0
        self.clip_task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self.sender_task: asyncio.Task | None = None
        self.levels_task: asyncio.Task | None = None
        self.program_holders: list[ProgramHolder] = []
        self.extra_live_scenes: set[str] = set()
        self._ready_info: list[Callable[[], Awaitable[dict] | dict]] = []
        # -- locks (the video lock also serialises output start/stop; plugins may share it)
        self.clip_lock = asyncio.Lock()
        self.video_lock = asyncio.Lock()
        # -- components
        self.broll = broll_module.Broll(self)
        self.clips = clips_module.Clips(self)
        self.preview = preview_module.Preview(self)
        self.levels = levels_module.Levels(self)
        self.app = web.Application(
            client_max_size=1024 * 1024 * 1024,
            middlewares=[self._errors],
        )
        self.app[CONTROLLER_KEY] = self
        self._add_routes()
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)
        call_hook(self.plugins, "register", self)

    # ------------------------------------------------------------------ plugin-facing helpers
    async def obs(self, *requests: tuple[str, dict]) -> list[dict]:
        """Send obs-websocket requests in order; the list of their response data."""
        return await self.obs_client.request(*requests)

    def add_program_holder(self, holder: ProgramHolder) -> None:
        self.program_holders.append(holder)

    def add_ready_info(self, provider: Callable[[], Awaitable[dict] | dict]) -> None:
        """Merge extra keys into GET /ready. ``provider`` may be sync or async."""
        self._ready_info.append(provider)

    def held_by(self) -> ProgramHolder | None:
        return next((holder for holder in self.program_holders if holder.is_held()), None)

    async def release_program_holders(self, neutral: bool = True) -> None:
        for holder in self.program_holders:
            if holder.is_held() and holder.release is not None:
                await holder.release(neutral)

    def sender_alive(self) -> bool:
        if not self.config.features.sender_lease:
            return True
        return datetime.now().timestamp() - self.sender_last_seen <= self.config.sender.lease_seconds

    @property
    def program_control(self) -> bool:
        return bool(self.config.features.program_control)

    @property
    def stopped_scene(self) -> str:
        return self.config.stopped.scene

    @property
    def live_camera(self) -> Section | None:
        return self.config.primary_camera

    def canvas_size(self) -> tuple[int, int]:
        """The program canvas: the active video profile's size, else the configured video size."""
        if self.program_control and self.config.features.video_profiles:
            profile = self.config.profiles[self.video_profile]
            return profile["width"], profile["height"]
        return self.config.video.width, self.config.video.height

    def scene_name(self, key: str) -> str | None:
        return self.config.scene_keys.get(key)

    def clean_display_name(self, value: str) -> str:
        return clips_module.clean_display_name(value)

    def identity(self, request: web.Request) -> str | None:
        """The identity the reverse proxy put in the configured header (None: no header configured or sent).

        Only used to keep per-user state apart; authentication and authorization are external."""
        header = self.config.access.identity_header
        return (request.headers.get(header) or None) if header else None

    # ------------------------------------------------------------------ middleware
    @web.middleware
    async def _errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except (ObsError, OSError, asyncio.TimeoutError, WebSocketException) as exc:
            # OBS is down, restarting or refused the request.
            return web.json_response({"ok": False, "error": str(exc) or type(exc).__name__}, status=503)

    # ------------------------------------------------------------------ routes
    def _add_routes(self) -> None:
        r = self.app.router
        if self.config.console.enabled:
            r.add_get("/", console_module.index(self))
        r.add_get("/health", self.health)
        r.add_get("/ready", self.ready)
        r.add_get("/api/scenes", self.scenes)
        r.add_get("/api/program-preview.jpg", self.preview.program_preview)
        r.add_get("/api/program-live.mjpg", self.preview.program_live)
        r.add_get("/media/3-minute-video.mp4", self.broll.media_preview)
        r.add_get("/api/clips", self.clips.list)
        r.add_get("/api/input-levels", self.levels.input_levels)
        r.add_post("/api/clips/upload", self.clips.upload)
        r.add_post("/api/clips/{clip_id}/trim", self.clips.trim)
        r.add_get("/clips/{clip_id}.mp4", self.clips.media)
        r.add_delete("/api/clips/{clip_id}", self.clips.delete)
        r.add_get("/api/recording/status", self.clips.recording_status)
        r.add_get("/api/daily-fallback/status", self.broll.status)
        r.add_get("/api/daily-fallback/preview", self.broll.preview)
        r.add_post("/api/daily-fallback/play", self.broll.play)
        r.add_post("/api/daily-fallback/cancel", self.broll.cancel)
        r.add_post("/api/scene/{key}", self.switch_scene)
        r.add_post("/api/clip/{clip_id}/scene", self.clips.switch)
        r.add_post("/api/clip/{clip_id}/play", self.clips.play)
        r.add_post("/api/call/live", self.call_live)
        r.add_post("/api/call/stop", self.call_stop)
        r.add_post("/api/call/microphone", self.microphone_gate)
        r.add_post("/api/call/clip-audio", self.clip_audio_gate)
        r.add_post("/api/call/video-profile", self.video_profile_route)
        r.add_post("/api/recording/start", self.clips.recording_start)
        r.add_post("/api/recording/stop", self.clips.recording_stop)
        r.add_post("/api/daily-fallback/ensure", self.broll.ensure)
        r.add_post("/api/sender-heartbeat", self.sender_heartbeat)
        r.add_post("/api/restart-live", self.restart_live)
        r.add_post("/api/restart-output", self.restart_output)
        r.add_post("/api/stop-output", self.stop_output)
        r.add_post("/api/start-output", self.start_output)

    # ------------------------------------------------------------------ health
    async def health(self, _: web.Request) -> web.Response:
        try:
            version, recording, scene = await self.obs(
                ("GetVersion", {}), ("GetRecordStatus", {}), ("GetCurrentProgramScene", {})
            )
            payload = {
                "healthy": bool(recording.get("outputActive")),
                "obs": version.get("obsVersion"),
                "scene": scene.get("currentProgramSceneName"),
                "output_active": bool(recording.get("outputActive")),
                "output_timecode": recording.get("outputTimecode"),
            }
            return web.json_response(payload, status=200 if payload["healthy"] else 503)
        except Exception as exc:
            return web.json_response({"healthy": False, "error": str(exc)}, status=503)

    async def ready(self, _: web.Request) -> web.Response:
        """Container readiness, independent of whether an operator started the output."""
        try:
            version = (await self.obs(("GetVersion", {})))[0]
            payload: dict[str, Any] = {
                "ready": True,
                "obsVersion": version.get("obsVersion"),
                "rpcVersion": version.get("rpcVersion"),
            }
            if self.program_control:
                scene = (await self.obs(("GetCurrentProgramScene", {})))[0]
                payload.update(
                    {
                        "mode": self.config.features.mode_label,
                        "program_audio_muted": self.program_audio_muted,
                        "clip_audio_muted": self.clip_audio_muted,
                        "program_scene": scene.get("currentProgramSceneName"),
                    }
                )
                if self.config.features.video_profiles:
                    payload.update(
                        {"video_profile": self.video_profile, "video_profiles": list(self.config.profiles)}
                    )
            for provider in self._ready_info:
                extra = provider()
                payload.update(await extra if asyncio.iscoroutine(extra) else extra)
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response({"ready": False, "error": str(exc)}, status=503)

    async def scenes(self, _: web.Request) -> web.Response:
        """New in obs-control: the OBS scenes, the configured scene keys and the program scene."""
        scene_list, current = await self.obs(("GetSceneList", {}), ("GetCurrentProgramScene", {}))
        return web.json_response(
            {
                "scenes": [scene["sceneName"] for scene in reversed(scene_list.get("scenes", []))],
                "program": current.get("currentProgramSceneName"),
                "keys": self.config.scene_keys,
                "stopped": self.stopped_scene,
            }
        )

    # ------------------------------------------------------------------ scene switching
    async def switch_scene(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if key not in self.config.scene_keys:
            raise web.HTTPNotFound()
        if self.program_control:
            if self.config.camera(key) is None:
                raise web.HTTPNotFound()
            return await self.go_live(key)
        if key == self.config.media.key:
            fallback = self.broll.today_path()
            if fallback is None:
                return web.json_response(
                    {"ok": False, "error": "Today's b-roll video is not ready yet"}, status=409
                )
            await self.broll.set_media_source(fallback)
        scene = self.config.scene_keys[key]
        await self.obs(("SetCurrentProgramScene", {"sceneName": scene}))
        return web.json_response({"ok": True, "scene": scene})

    async def go_live(self, key: str) -> web.Response:
        camera = self.config.camera(key)
        holder = self.held_by()
        if holder:
            return web.json_response({"ok": False, "error": holder.message}, status=409)
        if not self.sender_alive():
            await self.stop_program()
            return web.json_response(
                {"ok": False, "error": "Live sender is unavailable; keeping neutral output"}, status=409
            )
        await self.obs(("SetCurrentProgramScene", {"sceneName": camera.scene}))
        self.active_clip_input = None
        await self.set_program_audio_muted(self.program_audio_muted, strict=False)
        return web.json_response({"ok": True, "scene": camera.scene})

    async def call_live(self, request: web.Request) -> web.Response:
        if not self.program_control or self.live_camera is None:
            raise web.HTTPNotFound()
        return await self.go_live(self.live_camera.key)

    async def stop_program(self) -> dict[str, Any]:
        if self.held_by():
            await self.release_program_holders(neutral=True)  # stop always wins
        await self.ensure_stopped_scene()
        self.active_clip_input = None
        await self.obs(("SetCurrentProgramScene", {"sceneName": self.stopped_scene}))
        await self.set_program_audio_muted(True, strict=False)
        if self.clip_task and not self.clip_task.done():
            self.clip_task.cancel()
        return {"ok": True, "scene": self.stopped_scene, "audio_muted": True}

    async def call_stop(self, _: web.Request) -> web.Response:
        if not self.program_control:
            raise web.HTTPNotFound()
        return web.json_response(await self.stop_program())

    async def restart_live(self, request: web.Request) -> web.Response:
        camera = self.config.camera(request.query.get("key", "")) if request.query.get("key") else self.live_camera
        if camera is None:
            raise web.HTTPNotFound()
        try:
            await self.obs(("PressInputPropertiesButton", {"inputName": camera.source, "propertyName": "refreshnocache"}))
        except Exception:
            await self.obs(
                (
                    "SetInputSettings",
                    {
                        "inputName": camera.source,
                        "inputSettings": {"url": self.config.camera_url(camera)},
                        "overlay": True,
                    },
                )
            )
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ output
    async def restart_output(self, _: web.Request) -> web.Response:
        async with self.video_lock:
            (status,) = await self.obs(("GetRecordStatus", {}))
            if status.get("outputActive"):
                await self.obs(("StopRecord", {}))
                await asyncio.sleep(0.5)
            await self.obs(("StartRecord", {}))
        return web.json_response({"ok": True})

    async def stop_output(self, _: web.Request) -> web.Response:
        async with self.video_lock:
            (status,) = await self.obs(("GetRecordStatus", {}))
            if status.get("outputActive"):
                await self.obs(("StopRecord", {}))
        return web.json_response({"ok": True})

    async def start_output(self, _: web.Request) -> web.Response:
        async with self.video_lock:
            (status,) = await self.obs(("GetRecordStatus", {}))
            if not status.get("outputActive"):
                await self.obs(("StartRecord", {}))
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ stopped scene, audio
    async def ensure_stopped_scene(self) -> None:
        (scene_list,) = await self.obs(("GetSceneList", {}))
        if self.stopped_scene in {scene["sceneName"] for scene in scene_list.get("scenes", [])}:
            return
        profile = self.config.profiles[self.video_profile] if self.config.features.video_profiles else None
        width = profile["width"] if profile else self.config.video.width
        height = profile["height"] if profile else self.config.video.height
        await self.obs(
            ("CreateScene", {"sceneName": self.stopped_scene}),
            (
                "CreateInput",
                {
                    "sceneName": self.stopped_scene,
                    "inputName": self.config.stopped.source,
                    "inputKind": "color_source_v3",
                    "inputSettings": {"color": color_value(self.config.stopped.color), "width": width, "height": height},
                    "sceneItemEnabled": True,
                },
            ),
        )

    def camera_sources(self) -> dict[str, Section]:
        return {camera.source: camera for camera in self.config.cameras}

    async def set_program_audio_muted(self, muted: bool, strict: bool = True) -> int:
        prefix = f"{self.config.clips.source_prefix} · "
        cameras = self.camera_sources()
        (inputs,) = await self.obs(("GetInputList", {}))
        names = [
            item["inputName"]
            for item in inputs.get("inputs", [])
            if item["inputName"] in cameras or item["inputName"].startswith(prefix)
        ]
        current_scene = None
        changed = 0
        for name in names:
            if name in cameras:
                input_muted = muted or bool(self.active_clip_input)
                if not input_muted:
                    if current_scene is None:
                        (scene,) = await self.obs(("GetCurrentProgramScene", {}))
                        current_scene = scene.get("currentProgramSceneName")
                    # The mic switch is a preference; an unselected camera must stay silent.
                    input_muted = current_scene not in ({cameras[name].scene} | self.extra_live_scenes)
            else:
                # The mic gate covers the live inputs only; the active clip has its own sound
                # switch (on by default). Inactive clips stay silent.
                input_muted = name != self.active_clip_input or self.clip_audio_muted
            try:
                await self.obs(("SetInputMute", {"inputName": name, "inputMuted": input_muted}))
                changed += 1
            except Exception:
                if strict and name in cameras:
                    raise
        if strict and not changed:
            raise RuntimeError("No program audio source is available to gate")
        return changed

    async def _gate_body(self, request: web.Request) -> bool:
        try:
            return bool((await request.json()).get("muted", False))
        except Exception:
            return False

    async def microphone_gate(self, request: web.Request) -> web.Response:
        if not self.program_control:
            raise web.HTTPNotFound()
        muted = await self._gate_body(request)
        try:
            await self.set_program_audio_muted(muted)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not set program audio gate: {exc}"}, status=503)
        self.program_audio_muted = muted
        return web.json_response({"ok": True, "muted": muted})

    async def clip_audio_gate(self, request: web.Request) -> web.Response:
        if not self.program_control:
            raise web.HTTPNotFound()
        muted = await self._gate_body(request)
        previous, self.clip_audio_muted = self.clip_audio_muted, muted
        try:
            await self.set_program_audio_muted(self.program_audio_muted, strict=False)
        except Exception as exc:
            self.clip_audio_muted = previous
            return web.json_response({"ok": False, "error": f"Could not set clip sound: {exc}"}, status=503)
        return web.json_response({"ok": True, "muted": muted})

    # ------------------------------------------------------------------ video profiles
    @property
    def video_profile_file(self) -> Path:
        return self.state_dir / "video-profile.json"

    def saved_video_profile(self) -> str:
        try:
            name = json.loads(self.video_profile_file.read_text(encoding="utf-8")).get("profile")
        except (OSError, ValueError):
            return self.config.video.default_profile
        return name if name in self.config.profiles else self.config.video.default_profile

    async def fit_scene_to_canvas(self, scene_name: str, width: int, height: int) -> None:
        """Scale every item of a scene to fit the canvas (keeps aspect ratio, centred)."""
        (items,) = await self.obs(("GetSceneItemList", {"sceneName": scene_name}))
        for item in items.get("sceneItems", []):
            await self.obs(
                (
                    "SetSceneItemTransform",
                    {
                        "sceneName": scene_name,
                        "sceneItemId": item["sceneItemId"],
                        "sceneItemTransform": {
                            "boundsType": "OBS_BOUNDS_SCALE_INNER",
                            "boundsWidth": float(width),
                            "boundsHeight": float(height),
                            "boundsAlignment": 0,
                            "alignment": 5,
                            "positionX": 0.0,
                            "positionY": 0.0,
                        },
                    },
                )
            )

    async def apply_video_profile(self, name: str) -> dict[str, Any]:
        profile = self.config.profiles[name]
        width, height = profile["width"], profile["height"]
        async with self.video_lock:
            holder = self.held_by()
            if holder:
                raise ProgramBusy(holder.message)
            (status,) = await self.obs(("GetRecordStatus", {}))
            was_active = bool(status.get("outputActive"))
            if was_active:
                await self.obs(("StopRecord", {}))
                for _ in range(50):
                    (status,) = await self.obs(("GetRecordStatus", {}))
                    if not status.get("outputActive"):
                        break
                    await asyncio.sleep(0.1)
            try:
                requests: list[tuple[str, dict]] = [
                    (
                        "SetVideoSettings",
                        {"baseWidth": width, "baseHeight": height, "outputWidth": width, "outputHeight": height},
                    ),
                    (
                        "SetProfileParameter",
                        {"parameterCategory": "AdvOut", "parameterName": "FFVBitrate", "parameterValue": str(profile["encoder_kbps"])},
                    ),
                    # The program output follows the profile even if the canvas is temporarily
                    # larger (a plugin may switch the canvas).
                    ("SetProfileParameter", {"parameterCategory": "AdvOut", "parameterName": "FFRescale", "parameterValue": "true"}),
                    (
                        "SetProfileParameter",
                        {"parameterCategory": "AdvOut", "parameterName": "FFRescaleRes", "parameterValue": f"{width}x{height}"},
                    ),
                ]
                # When any profile edits the camera URL, every switch sets it (a plain profile must
                # undo the previous profile's query); otherwise the URL is left alone (no reload).
                edits_url = any(p["camera_query"] or p["camera_query_drop"] for p in self.config.profiles.values())
                for camera in self.config.cameras:
                    settings: dict[str, Any] = {"width": width, "height": height}
                    if edits_url:
                        settings["url"] = camera_profile_url(self.config.camera_url(camera), profile)
                    requests.append(("SetInputSettings", {"inputName": camera.source, "overlay": True, "inputSettings": settings}))
                requests.append(
                    (
                        "SetInputSettings",
                        {"inputName": self.config.stopped.source, "overlay": True, "inputSettings": {"width": width, "height": height}},
                    )
                )
                await self.obs(*requests)
                (scene_list,) = await self.obs(("GetSceneList", {}))
                for scene in scene_list.get("scenes", []):
                    await self.fit_scene_to_canvas(scene["sceneName"], width, height)
            finally:
                if was_active:
                    await self.obs(("StartRecord", {}))
            self.video_profile = name
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.video_profile_file.write_text(json.dumps({"profile": name}), encoding="utf-8")
        return {"ok": True, "profile": name, **{k: profile[k] for k in ("width", "height", "encoder_kbps", "camera_query")}}

    async def video_profile_route(self, request: web.Request) -> web.Response:
        if not (self.program_control and self.config.features.video_profiles):
            raise web.HTTPNotFound()
        try:
            name = (await request.json()).get("profile")
        except Exception:
            name = None
        if name not in self.config.profiles:
            return web.json_response(
                {"ok": False, "error": f"Profile must be one of {', '.join(self.config.profiles)}"}, status=400
            )
        try:
            return web.json_response(await self.apply_video_profile(name))
        except ProgramBusy as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not change video quality: {exc}"}, status=503)

    # ------------------------------------------------------------------ sender lease
    async def sender_heartbeat(self, request: web.Request) -> web.Response:
        program_control = self.program_control
        try:
            body = await request.json()
            healthy = body.get("healthy", not program_control)
            if healthy and self.config.access.identity_header:
                self.sender_user = self.identity(request)
        except Exception:
            healthy = not program_control
        self.sender_last_seen = datetime.now().timestamp() if healthy else 0
        if program_control:
            return web.json_response({"ok": True, "daily_fallback": "manual"})
        self.broll.auto_start()
        return web.json_response(self.broll.payload())

    async def _stop_stale_live_program(self) -> None:
        if self.sender_alive():
            return
        async with self.clip_lock:
            # Recheck after the lock: a new heartbeat or clip selection may have arrived.
            if self.sender_alive():
                return
            scene = (await self.obs(("GetCurrentProgramScene", {})))[0]
            if scene.get("currentProgramSceneName") in {camera.scene for camera in self.config.cameras}:
                await self.stop_program()

    async def _monitor_sender(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self._stop_stale_live_program()
            except Exception:
                pass  # OBS may be restarting; retry once its control socket returns

    # ------------------------------------------------------------------ startup
    async def _initialize_program(self) -> None:
        if not self.program_control:
            return
        for _attempt in range(30):
            try:
                await self.ensure_stopped_scene()
                restore = json.loads(os.environ.get(ENV_RESTORE, "null"))
                use_profiles = self.config.features.video_profiles
                if isinstance(restore, dict):
                    # A targeted controller restart keeps the running OBS program and camera peer.
                    self.program_audio_muted = bool(restore.get("program_audio_muted", False))
                    self.clip_audio_muted = bool(restore.get("clip_audio_muted", False))
                    self.active_clip_input = restore.get("active_clip_input")
                    if use_profiles:
                        self.video_profile = self.saved_video_profile()
                    self.sender_last_seen = datetime.now().timestamp() if restore.get("sender_healthy") else 0
                    self.sender_user = restore.get("user")
                    await self.set_program_audio_muted(self.program_audio_muted, strict=False)
                    return
                await self.obs(("SetCurrentProgramScene", {"sceneName": self.stopped_scene}))
                self.active_clip_input = None
                self.program_audio_muted = False
                await self.set_program_audio_muted(True, strict=False)
                if use_profiles:
                    await self.apply_video_profile(self.saved_video_profile())
                return
            except Exception:
                await asyncio.sleep(1)

    async def _ensure_output_after_startup(self) -> None:
        """Start the output once OBS has a renderable program frame.

        ``--startrecording`` can run before browser/media sources have produced a first frame; FFmpeg
        then exits with zero frames while the container still looks ready. Retry only during startup.
        """
        if not self.config.output.autostart:
            return
        for _attempt in range(90):
            try:
                (status,) = await self.obs(("GetRecordStatus", {}))
                if status.get("outputActive"):
                    return
                await self.obs(("StartRecord", {}))
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _wait_for_obs(self, seconds: int = 180) -> None:
        for _ in range(seconds):
            try:
                await self.obs(("GetVersion", {}))
                return
            except Exception:
                await asyncio.sleep(1)

    async def _startup_sequence(self) -> None:
        await self._wait_for_obs()
        await self._initialize_program()
        await self.clips.restore_scenes()
        await self.broll.restore()
        await self._ensure_output_after_startup()

    async def _on_startup(self, app: web.Application) -> None:
        # Background: the HTTP server answers (503 on /ready) while OBS is still starting.
        self.startup_task = asyncio.create_task(self._startup_sequence())
        if self.program_control and self.config.features.sender_lease:
            self.sender_task = asyncio.create_task(self._monitor_sender())
        if self.program_control:
            self.levels_task = asyncio.create_task(self.levels.monitor())

    async def _on_cleanup(self, app: web.Application) -> None:
        if self.clip_task is not None and not self.clip_task.done():
            self.clip_task.cancel()
        for task in (self.startup_task, self.sender_task, self.levels_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.broll.shutdown()
        await self.preview.shutdown()


def build_controller(config: Config | None = None) -> Controller:
    config = config or load_config()
    return Controller(config, load_plugins(config))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    controller = build_controller()
    web.run_app(
        controller.app, host=controller.config.http.bind, port=controller.config.http.port, access_log=None
    )


if __name__ == "__main__":
    main()
