# SPDX-License-Identifier: Apache-2.0
"""Recorded clips (upload, trim, play, delete) and the manual recording of the program output."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession, ClientTimeout, web

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

SAFE_CLIP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


def clean_display_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()[:60]
    return cleaned or "Recorded clip"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]
    return slug or "clip"


async def probe_duration(path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    duration = float(output.decode().strip())
    if process.returncode or duration <= 0:
        raise ValueError("not a playable video")
    return duration


class Clips:
    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        self.finalize_lock = asyncio.Lock()
        self.finalizing_capture: str | None = None

    # ------------------------------------------------------------------ naming and metadata
    @property
    def directory(self) -> Path:
        return self.ctl.recordings_dir

    @property
    def scene_prefix(self) -> str:
        return self.ctl.config.clips.scene_prefix

    def input_name(self, clip_id: str) -> str:
        return f"{self.ctl.config.clips.source_prefix} · {clip_id}"

    def metadata(self, clip_path: Path) -> dict[str, Any]:
        metadata_path = clip_path.with_suffix(".json")
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                metadata = {}
        clip_id = clip_path.stem
        display_name = metadata.get("display_name") or clip_id
        stat = clip_path.stat()
        return {
            "id": clip_id,
            "display_name": display_name,
            "scene_name": metadata.get("scene_name") or f"{self.scene_prefix} · {display_name}",
            "created_at": metadata.get("created_at") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "bytes": stat.st_size,
            "url": f"clips/{clip_id}.mp4",
            "path": str(clip_path),
        }

    def all(self) -> list[dict[str, Any]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        clips = [self.metadata(path) for path in self.directory.glob("*.mp4")]
        return sorted(clips, key=lambda clip: clip["created_at"], reverse=True)

    def _path(self, request: web.Request) -> tuple[str, Path]:
        clip_id = request.match_info["clip_id"]
        if not SAFE_CLIP_ID.fullmatch(clip_id):
            raise web.HTTPNotFound()
        path = self.directory / f"{clip_id}.mp4"
        if not path.is_file():
            raise web.HTTPNotFound()
        return clip_id, path

    # ------------------------------------------------------------------ OBS scenes for clips
    async def ensure_scene(self, clip: dict[str, Any]) -> None:
        ctl = self.ctl
        (scene_list,) = await ctl.obs(("GetSceneList", {}))
        if clip["scene_name"] in {scene["sceneName"] for scene in scene_list.get("scenes", [])}:
            return
        await ctl.obs(("CreateScene", {"sceneName": clip["scene_name"]}))
        await ctl.obs(
            (
                "CreateInput",
                {
                    "sceneName": clip["scene_name"],
                    "inputName": self.input_name(clip["id"]),
                    "inputKind": "ffmpeg_source",
                    "inputSettings": {
                        "local_file": clip["path"],
                        "looping": not ctl.program_control,
                        "restart_on_activate": True,
                        "close_when_inactive": ctl.program_control,
                        "clear_on_media_end": False,
                        "hw_decode": False,
                    },
                    "sceneItemEnabled": True,
                },
            )
        )
        if ctl.program_control:
            width, height = ctl.canvas_size()
            await ctl.fit_scene_to_canvas(clip["scene_name"], width, height)

    async def restore_scenes(self) -> None:
        for _attempt in range(30):
            try:
                for clip in self.all():
                    await self.ensure_scene(clip)
                return
            except Exception:
                await asyncio.sleep(1)

    async def _end_after(self, scene_name: str, duration: float) -> None:
        ctl = self.ctl
        try:
            await asyncio.sleep(duration + 0.15)
            async with ctl.clip_lock:
                current = (await ctl.obs(("GetCurrentProgramScene", {})))[0]
                if current.get("currentProgramSceneName") == scene_name:
                    if ctl.sender_alive() and ctl.live_camera is not None:
                        await ctl.obs(("SetCurrentProgramScene", {"sceneName": ctl.live_camera.scene}))
                        ctl.active_clip_input = None
                        await ctl.set_program_audio_muted(ctl.program_audio_muted, strict=False)
                    else:
                        await ctl.ensure_stopped_scene()
                        ctl.active_clip_input = None
                        await ctl.obs(("SetCurrentProgramScene", {"sceneName": ctl.stopped_scene}))
                        await ctl.set_program_audio_muted(True, strict=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if ctl.clip_task is asyncio.current_task():
                ctl.clip_task = None

    # ------------------------------------------------------------------ routes
    async def list(self, _: web.Request) -> web.Response:
        return web.json_response({"clips": self.all()})

    async def media(self, request: web.Request) -> web.StreamResponse:
        _, path = self._path(request)
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    async def switch(self, request: web.Request) -> web.Response:
        if self.ctl.program_control:
            raise web.HTTPNotFound()
        _, path = self._path(request)
        clip = self.metadata(path)
        await self.ensure_scene(clip)
        await self.ctl.obs(("SetCurrentProgramScene", {"sceneName": clip["scene_name"]}))
        return web.json_response({"ok": True, "scene": clip["scene_name"]})

    async def play(self, request: web.Request) -> web.Response:
        ctl = self.ctl
        if not ctl.program_control:
            raise web.HTTPNotFound()
        clip_id, path = self._path(request)
        holder = ctl.held_by()
        if holder:
            return web.json_response({"ok": False, "error": holder.message}, status=409)
        try:
            duration = await probe_duration(path)
        except Exception:
            return web.json_response({"ok": False, "error": "Could not read clip duration"}, status=422)
        try:
            body = await request.json()
        except Exception:
            body = {}
        looping = bool(body.get("loop", False))
        name = self.input_name(clip_id)
        async with ctl.clip_lock:
            if ctl.clip_task and not ctl.clip_task.done():
                ctl.clip_task.cancel()
                ctl.clip_task = None
            clip = self.metadata(path)
            await self.ensure_scene(clip)
            await ctl.obs(
                (
                    "SetInputSettings",
                    {
                        "inputName": name,
                        "inputSettings": {
                            "local_file": str(path),
                            "looping": looping,
                            "restart_on_activate": True,
                            "close_when_inactive": True,
                            "clear_on_media_end": False,
                            "hw_decode": False,
                        },
                        "overlay": True,
                    },
                )
            )
            ctl.active_clip_input = name
            await ctl.set_program_audio_muted(ctl.program_audio_muted, strict=False)
            await ctl.obs(
                ("SetCurrentProgramScene", {"sceneName": clip["scene_name"]}),
                ("TriggerMediaInputAction", {"inputName": name, "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"}),
            )
            if not looping:
                ctl.clip_task = asyncio.create_task(self._end_after(clip["scene_name"], duration))
        return web.json_response({"ok": True, "clip_id": clip_id, "duration": duration, "loop": looping})

    async def upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        display_name = "Uploaded clip"
        upload_path = clip_path = None
        try:
            while field := await reader.next():
                if field.name == "name" and not field.filename:
                    display_name = clean_display_name(await field.text())
                elif field.name == "file" and field.filename:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    clip_id = f"upload-{uuid.uuid4().hex[:16]}"
                    upload_path = self.directory / f".{clip_id}.upload"
                    clip_path = self.directory / f"{clip_id}.mp4"
                    total = 0
                    with upload_path.open("xb") as target:
                        while chunk := await field.read_chunk(size=1024 * 1024):
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise ValueError("Upload exceeds the 1 GiB limit")
                            target.write(chunk)
            if upload_path is None or clip_path is None:
                raise ValueError("Choose one video file to upload")
            try:
                duration = await probe_duration(upload_path)
            except Exception:
                raise ValueError("Uploaded file is not a playable video") from None
            os.replace(upload_path, clip_path)
            record = {
                "id": clip_path.stem,
                "display_name": display_name,
                "scene_name": f"{self.scene_prefix} · {display_name} · {clip_path.stem[-8:]}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "upload",
            }
            clip_path.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            clip = self.metadata(clip_path)
            await self.ensure_scene(clip)
            return web.json_response({"ok": True, "clip": clip, "duration": duration}, status=201)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not import clip: {exc}"}, status=500)
        finally:
            if upload_path:
                upload_path.unlink(missing_ok=True)

    async def trim(self, request: web.Request) -> web.Response:
        clip_id, source = self._path(request)
        partial = None
        try:
            data = await request.json()
            start = float(data.get("start_seconds", 0))
            end = float(data["end_seconds"])
            if start < 0 or end <= start:
                raise ValueError("Trim end must be after trim start")
            try:
                duration = await probe_duration(source)
            except Exception:
                raise ValueError("Trim points must be within the clip duration") from None
            if end > duration:
                raise ValueError("Trim points must be within the clip duration")
            new_id = f"trim-{uuid.uuid4().hex[:16]}"
            target = self.directory / f"{new_id}.mp4"
            partial = self.directory / f".{new_id}.partial.mp4"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
                "-i", str(source), "-t", str(end - start), "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                "-movflags", "+faststart", str(partial),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            if process.returncode:
                raise RuntimeError(stderr.decode("utf-8", errors="replace") or "FFmpeg trim failed")
            os.replace(partial, target)
            original = self.metadata(source)
            display_name = clean_display_name(str(data.get("name") or f"{original['display_name']} trimmed"))
            record = {
                "id": new_id,
                "display_name": display_name,
                "scene_name": f"{self.scene_prefix} · {display_name} · {new_id[-8:]}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "trim_of": clip_id,
                "trim_start_seconds": start,
                "trim_end_seconds": end,
            }
            target.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            clip = self.metadata(target)
            await self.ensure_scene(clip)
            return web.json_response({"ok": True, "clip": clip, "duration": end - start}, status=201)
        except (KeyError, TypeError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not trim clip: {exc}"}, status=500)
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)

    async def delete(self, request: web.Request) -> web.Response:
        ctl = self.ctl
        if not ctl.program_control:
            raise web.HTTPNotFound()
        clip_id, path = self._path(request)
        clip = self.metadata(path)
        name = self.input_name(clip_id)
        async with ctl.clip_lock:
            try:
                (current,) = await ctl.obs(("GetCurrentProgramScene", {}))
            except Exception as exc:
                return web.json_response({"ok": False, "error": f"OBS unavailable: {exc}"}, status=503)
            if current.get("currentProgramSceneName") == clip["scene_name"] or ctl.active_clip_input == name:
                return web.json_response(
                    {"ok": False, "error": "This clip is being sent to the call; return to live before deleting it"},
                    status=409,
                )
            for request_type, data in (
                ("RemoveScene", {"sceneName": clip["scene_name"]}),
                ("RemoveInput", {"inputName": name}),
            ):
                try:
                    await ctl.obs((request_type, data))
                except Exception:
                    pass  # the scene/input may never have been created; the file is the source of truth
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
        return web.json_response({"ok": True, "deleted": clip_id})

    # ------------------------------------------------------------------ manual recording (via the relay)
    async def relay_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.request(method, f"{self.ctl.config.relay.control_url.rstrip('/')}{path}", **kwargs) as response:
                try:
                    payload = await response.json()
                except Exception:
                    payload = {"error": await response.text()}
                if response.status >= 400:
                    raise RuntimeError(payload.get("error") or f"Relay returned {response.status}")
                return payload

    async def download_capture(self, capture_id: str, target: Path) -> None:
        url = f"{self.ctl.config.relay.control_url.rstrip('/')}/recording/file/{capture_id}"
        async with ClientSession(timeout=ClientTimeout(total=300)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"Could not download capture ({response.status})")
                with target.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        handle.write(chunk)

    async def recording_status(self, _: web.Request) -> web.Response:
        try:
            payload = await self.relay_request("GET", "/recording/status")
        except Exception as exc:
            return web.json_response({"recording": False, "available": False, "error": str(exc)}, status=503)
        payload["available"] = True
        payload["finalizing"] = self.finalizing_capture
        return web.json_response(payload)

    async def recording_start(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        display_name = clean_display_name(str(data.get("name", "")))
        capture_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(display_name)}"
        self.directory.mkdir(parents=True, exist_ok=True)
        pending = self.directory / f".{capture_id}.pending.json"
        pending.write_text(json.dumps({"display_name": display_name}, indent=2), encoding="utf-8")
        try:
            payload = await self.relay_request("POST", "/recording/start", json={"capture_id": capture_id})
        except Exception as exc:
            pending.unlink(missing_ok=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        return web.json_response({"ok": True, "display_name": display_name, **payload})

    async def recording_stop(self, _: web.Request) -> web.Response:
        if self.finalize_lock.locked():
            return web.json_response({"ok": False, "error": "A recording is already being prepared"}, status=409)
        async with self.finalize_lock:
            try:
                capture = await self.relay_request("POST", "/recording/stop")
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=409)
            capture_id = capture["capture_id"]
            self.finalizing_capture = capture_id
            self.directory.mkdir(parents=True, exist_ok=True)
            transport = self.directory / f".{capture_id}.capture.ts"
            partial = self.directory / f".{capture_id}.partial.mp4"
            final = self.directory / f"{capture_id}.mp4"
            pending = self.directory / f".{capture_id}.pending.json"
            try:
                await self.download_capture(capture_id, transport)
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts+discardcorrupt", "-i", str(transport),
                    "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(partial),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await process.communicate()
                if process.returncode != 0:
                    raise RuntimeError(
                        stderr.decode("utf-8", errors="replace").strip() or "FFmpeg could not prepare the recording"
                    )
                os.replace(partial, final)
                info = json.loads(pending.read_text(encoding="utf-8")) if pending.is_file() else {}
                display_name = clean_display_name(info.get("display_name", capture_id))
                record = {
                    "id": capture_id,
                    "display_name": display_name,
                    "scene_name": f"{self.scene_prefix} · {display_name} · {capture_id[:15]}",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                final.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                clip = self.metadata(final)
                await self.ensure_scene(clip)
                await self.relay_request("DELETE", f"/recording/file/{capture_id}")
                return web.json_response({"ok": True, "clip": clip})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=500)
            finally:
                self.finalizing_capture = None
                transport.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
