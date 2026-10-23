# SPDX-License-Identifier: Apache-2.0
"""Automatic b-roll: a short, quiet and steady video assembled each day from the live camera.

While a sender is present (the sender lease, see POST /api/sender-heartbeat) the program output is
captured in chunks through the UDP relay, the quiet and steady spans are selected with ffmpeg and
joined into one video. That video is the media scene's source: a fallback to switch to when the
live camera is not available. In program-control mode capture is started explicitly instead
(POST /api/daily-fallback/ensure) and only while the live scene is on air.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from urllib.parse import quote

from aiohttp import web

from .daily_video import assemble, exclude_voice_activity, find_clean_spans, usable_duration

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller


def owner_dirname(identity: str) -> str:
    """A directory name for any identity the proxy passes: percent-encoded, never ``.`` or ``..``."""
    return quote(identity, safe="").replace(".", "%2E") if identity.strip(".") == "" else quote(identity, safe="")


def local_day() -> str:
    return datetime.now().astimezone().date().isoformat()


class Broll:
    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.state: dict[str, Any] = {"state": "idle"}
        self.capture_user: str | None = None
        self.voice_started: float | None = None
        self.voice_markers: list[float] = []

    # ------------------------------------------------------------------ settings and paths
    @property
    def cfg(self):
        return self.ctl.config.broll

    @property
    def directory(self) -> Path:
        return self.ctl.recordings_dir / "daily-fallback"

    @property
    def target_seconds(self) -> int:
        return max(10, int(self.cfg.seconds))

    @property
    def chunk_seconds(self) -> int:
        return max(30, int(self.cfg.chunk_seconds))

    @property
    def max_seconds(self) -> int:
        return max(self.chunk_seconds, int(self.cfg.max_seconds))

    @property
    def min_segment(self) -> float:
        return max(3.0, float(self.cfg.min_segment_seconds))

    def path(self, day: str | None = None, user: str | None = None) -> Path:
        directory = self.directory / owner_dirname(user) if user else self.directory
        return directory / f"{day or local_day()}.mp4"

    def today_path(self, user: str | None = None) -> Path | None:
        path = self.path(user=user)
        return path if path.is_file() else None

    def user(self, request: web.Request | None) -> str | None:
        """The b-roll owner. Only in program control, with an identity header configured, is b-roll per identity."""
        if not self.ctl.config.per_identity_broll or not self.ctl.program_control:
            return None
        identity = self.ctl.identity(request) if request is not None else None
        if not identity:
            raise web.HTTPBadRequest(text="An authenticated b-roll owner is required")
        return identity

    # ------------------------------------------------------------------ OBS
    async def set_media_source(self, path: Path) -> None:
        await self.ctl.obs(
            (
                "SetInputSettings",
                {
                    "inputName": self.ctl.config.media.source,
                    "inputSettings": {
                        "local_file": str(path),
                        "looping": True,
                        "restart_on_activate": True,
                        "close_when_inactive": False,
                        "clear_on_media_end": False,
                        "hw_decode": False,
                    },
                    "overlay": True,
                },
            )
        )

    async def capture_ready(self, user: str | None) -> bool:
        ctl = self.ctl
        if not ctl.sender_alive():
            return False
        if not ctl.program_control:
            return True
        if ctl.sender_user != user or ctl.held_by() or ctl.live_camera is None:
            return False
        scene = (await ctl.obs(("GetCurrentProgramScene", {})))[0]
        return scene.get("currentProgramSceneName") == ctl.live_camera.scene

    async def _cleanup_stale_capture(self, day: str, user: str | None) -> None:
        try:
            relay = await self.ctl.clips.relay_request("GET", "/recording/status")
            prefix = f"daily-{day.replace('-', '')}-{user + '-' if user else ''}"
            if relay.get("recording") and str(relay.get("capture_id", "")).startswith(prefix):
                capture = await self.ctl.clips.relay_request("POST", "/recording/stop")
                await self.ctl.clips.relay_request("DELETE", f"/recording/file/{capture['capture_id']}")
        except Exception:
            pass

    # ------------------------------------------------------------------ the capture task
    async def _create(self, day: str, user: str | None) -> None:
        ctl = self.ctl
        relay_call = ctl.clips.relay_request
        program_control = ctl.program_control
        final_path = self.path(day, user)
        work_dir = final_path.parent / f".{day}.work"
        async with self.lock:
            self.capture_user = user
            if final_path.is_file():
                if not program_control:
                    await self.set_media_source(final_path)
                self.state = {"state": "ready", "day": day}
                return
            try:
                # A fresh sender means the live feed should be on air. This also prevents
                # yesterday's fallback from staying active while the new one is being made.
                if not program_control and ctl.live_camera is not None:
                    await ctl.obs(("SetCurrentProgramScene", {"sceneName": ctl.live_camera.scene}))
                await self._cleanup_stale_capture(day, user)
                work_dir.mkdir(parents=True, exist_ok=True)
                sources: list[tuple[Path, list[tuple[float, float]]]] = []
                captured = 0
                usable = 0.0
                part = 0
                while captured < self.max_seconds and usable < self.target_seconds:
                    if program_control and local_day() != day:
                        self.state = {"state": "expired", "day": day}
                        return
                    if not await self.capture_ready(user):
                        self.state = {
                            "state": "waiting_live" if program_control else "waiting_sender",
                            "day": day,
                            "captured_seconds": captured,
                            "usable_seconds": int(usable),
                        }
                        await asyncio.sleep(3)
                        continue
                    relay = await relay_call("GET", "/recording/status")
                    if relay.get("recording"):
                        self.state = {
                            "state": "waiting",
                            "day": day,
                            "reason": "Waiting for the current manual recording to finish",
                        }
                        await asyncio.sleep(5)
                        continue
                    part += 1
                    generation = ctl.program_scene_generation
                    capture_id = f"daily-{day.replace('-', '')}-{user + '-' if user else ''}{part:02d}"
                    relay = await relay_call("POST", "/recording/start", json={"capture_id": capture_id})
                    started_at = float(relay.get("started_at", datetime.now().timestamp()))
                    self.voice_started = started_at
                    self.voice_markers = []
                    self.state = {
                        "state": "recording",
                        "day": day,
                        "started_at": started_at,
                        "captured_seconds": captured,
                        "usable_seconds": int(usable),
                        "max_seconds": self.max_seconds,
                    }
                    sender_lost = False
                    while datetime.now().timestamp() - started_at < self.chunk_seconds:
                        await asyncio.sleep(3)
                        if not await self.capture_ready(user) or (
                            program_control and generation != ctl.program_scene_generation
                        ):
                            sender_lost = True
                            break
                    await relay_call("POST", "/recording/stop")
                    self.voice_started = None
                    markers = list(self.voice_markers)
                    if program_control and generation != ctl.program_scene_generation:
                        sender_lost = True
                    captured += int(min(self.chunk_seconds, datetime.now().timestamp() - started_at))
                    capture_path = work_dir / f"{capture_id}.ts"
                    if not sender_lost:
                        self.state = {
                            "state": "analyzing",
                            "day": day,
                            "captured_seconds": captured,
                            "usable_seconds": int(usable),
                        }
                        await ctl.clips.download_capture(capture_id, capture_path)
                        spans = await find_clean_spans(
                            capture_path,
                            silence_db=float(self.cfg.silence_db),
                            motion_threshold=float(self.cfg.motion_threshold),
                            minimum=self.min_segment,
                        )
                        if program_control:
                            spans = exclude_voice_activity(spans, markers, minimum=self.min_segment)
                        sources.append((capture_path, spans))
                        usable = usable_duration(sources)
                    await relay_call("DELETE", f"/recording/file/{capture_id}")

                self.state = {
                    "state": "preparing",
                    "day": day,
                    "captured_seconds": captured,
                    "usable_seconds": int(usable),
                }
                result = await assemble(
                    sources,
                    final_path,
                    target_seconds=self.target_seconds,
                    size=(ctl.config.video.width, ctl.config.video.height),
                )
                if not result["ready"]:
                    self.state = {"state": "needs_more_input", "day": day, "captured_seconds": captured, **result}
                    return
                if not program_control:
                    await self.set_media_source(final_path)
                self.state = {
                    "state": "ready",
                    "day": day,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **result,
                }
            except Exception as exc:
                self.state = {"state": "error", "day": day, "error": str(exc)}
            finally:
                self.voice_started = None
                await self._cleanup_stale_capture(day, user)
                if work_dir.exists():
                    for work_file in work_dir.iterdir():
                        work_file.unlink(missing_ok=True)
                    work_dir.rmdir()

    def payload(self, user: str | None = None) -> dict[str, Any]:
        if not self.cfg.enabled:
            return {"enabled": False, "state": "disabled", "ready": False}
        day = local_day()
        path = self.path(day, user)
        if path.is_file():
            default_url = "api/daily-fallback/preview" if self.ctl.program_control else "media/3-minute-video.mp4"
            return {
                "state": "ready",
                "enabled": True,
                "day": day,
                "ready": True,
                "bytes": path.stat().st_size,
                "url": self.cfg.preview_url or default_url,
            }
        state = self.state if self.state.get("day") == day and self.capture_user == user else {"state": "idle"}
        payload = {"enabled": True, "day": day, "ready": False, "target_seconds": self.target_seconds, **state}
        if payload.get("state") == "recording":
            payload["elapsed"] = max(0, int(datetime.now().timestamp() - float(payload.get("started_at", 0))))
        return payload

    def _start(self, day: str, user: str | None) -> None:
        self.capture_user = user
        self.state = {"state": "starting", "day": day, "captured_seconds": 0, "usable_seconds": 0}
        self.task = asyncio.create_task(self._create(day, user))

    def _idle(self) -> bool:
        return self.task is None or self.task.done()

    def auto_start(self) -> None:
        """Heartbeat-driven start (default mode): make today's video if there is none yet."""
        if not (self.cfg.enabled and self.cfg.auto) or self.ctl.program_control:
            return
        day = local_day()
        if not self.path(day).is_file() and self._idle():
            self.capture_user = None
            self.task = asyncio.create_task(self._create(day, None))

    async def restore(self) -> None:
        """After a (re)start: point the media scene at today's video, or leave a stale one."""
        ctl = self.ctl
        if ctl.program_control or not self.cfg.enabled:
            return
        for _attempt in range(30):
            try:
                today = self.today_path()
                if today is not None:
                    await self.set_media_source(today)
                else:
                    (scene,) = await ctl.obs(("GetCurrentProgramScene", {}))
                    if scene.get("currentProgramSceneName") == ctl.config.media.scene and ctl.live_camera:
                        await ctl.obs(("SetCurrentProgramScene", {"sceneName": ctl.live_camera.scene}))
                break
            except Exception:
                await asyncio.sleep(1)
        if not ctl.config.features.sender_lease:
            self.auto_start()  # nobody will send heartbeats: capture from the start

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ routes
    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(self.payload(self.user(request)))

    async def ensure(self, request: web.Request) -> web.Response:
        ctl = self.ctl
        if not self.cfg.enabled:
            return web.json_response({"error": "Automatic b-roll is disabled"}, status=409)
        user = self.user(request)
        day = local_day()
        if ctl.program_control and not await self.capture_ready(user):
            return web.json_response(
                {"error": "Connect your camera and select Live before capturing today's b-roll"}, status=409
            )
        if not self._idle() and (self.capture_user != user or self.state.get("day") != day):
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if not self.path(day, user).is_file() and self._idle():
            self._start(day, user)
        return web.json_response(self.payload(user), status=202)

    async def cancel(self, request: web.Request) -> web.Response:
        user = self.user(request)
        if self.capture_user == user and not self._idle():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.state = {"state": "cancelled", "day": local_day()}
        return web.json_response(self.payload(user))

    async def play(self, request: web.Request) -> web.Response:
        ctl = self.ctl
        user = self.user(request)
        path = self.today_path(user)
        if not path:
            return web.json_response({"error": "Today's b-roll is not ready"}, status=409)
        holder = ctl.held_by()
        if holder:
            return web.json_response({"error": holder.message}, status=409)
        media = ctl.config.media
        async with ctl.clip_lock:
            if ctl.clip_task and not ctl.clip_task.done():
                ctl.clip_task.cancel()
                ctl.clip_task = None
            await self.set_media_source(path)
            ctl.active_clip_input = media.source
            await ctl.set_program_audio_muted(ctl.program_audio_muted, strict=False)
            await ctl.obs(
                ("SetInputMute", {"inputName": media.source, "inputMuted": True}),
                ("SetCurrentProgramScene", {"sceneName": media.scene}),
                ("TriggerMediaInputAction", {"inputName": media.source, "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"}),
            )
        return web.json_response({"ok": True, "scene": media.scene})

    async def preview(self, request: web.Request) -> web.StreamResponse:
        path = self.today_path(self.user(request))
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def media_preview(self, _: web.Request) -> web.StreamResponse:
        path = self.today_path()
        if path is None:
            raise web.HTTPNotFound(text="Today's b-roll video is not ready")
        return web.FileResponse(path, headers={"Cache-Control": "no-store, max-age=0"})
