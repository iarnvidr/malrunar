# SPDX-License-Identifier: Apache-2.0
"""Pre-fader input peak levels, so a console can show whether audio reaches OBS at all."""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

WINDOW_SECONDS = 1.5
EVENT_INPUT_VOLUME_METERS = 1 << 16
EVENT_SCENES = 1 << 2


def level_db(levels: list) -> float:
    """Pre-fader peak (inputPeak) across channels, in dBFS; -100 for digital silence."""
    peak = max((channel[2] for channel in levels if len(channel) > 2), default=0.0)
    return round(20 * math.log10(peak), 1) if peak > 0 else -100.0


class Levels:
    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        self.samples: dict[str, list[tuple[float, float]]] = {}

    def record(self, inputs: list[dict[str, Any]], now: float) -> None:
        ctl = self.ctl
        live = ctl.live_camera.source if ctl.live_camera else None
        threshold = float(ctl.config.broll.silence_db)
        for item in inputs:
            name = item.get("inputName")
            if not name:
                continue
            samples = self.samples.setdefault(name, [])
            db = level_db(item.get("inputLevelsMul") or [])
            samples.append((now, db))
            if name == live and ctl.broll.voice_started is not None and db > threshold:
                at = max(0, datetime.now().timestamp() - ctl.broll.voice_started)
                markers = ctl.broll.voice_markers
                if not markers or at - markers[-1] >= 0.1:
                    markers.append(at)
            while samples and now - samples[0][0] > WINDOW_SECONDS:
                samples.pop(0)

    def level(self, name: str | None, now: float) -> dict[str, float] | None:
        values = [db for at, db in self.samples.get(name or "", []) if now - at <= WINDOW_SECONDS]
        return {"peak_db": max(values)} if values else None

    async def monitor(self) -> None:
        """Keep recent peak levels; also counts program-scene changes for the b-roll capture."""
        ctl = self.ctl
        while True:
            try:
                async with ctl.obs_client.session(EVENT_INPUT_VOLUME_METERS | EVENT_SCENES) as socket:
                    ctl.program_scene_generation += 1
                    async for raw in socket:
                        message = json.loads(raw)
                        data = message.get("d", {})
                        if message.get("op") != 5:
                            continue
                        if data.get("eventType") == "InputVolumeMeters":
                            self.record(data.get("eventData", {}).get("inputs", []), time.monotonic())
                        elif data.get("eventType") == "CurrentProgramSceneChanged":
                            ctl.program_scene_generation += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                ctl.program_scene_generation += 1
                self.samples.clear()
                await asyncio.sleep(2)

    async def input_levels(self, _: web.Request) -> web.Response:
        ctl = self.ctl
        if not ctl.program_control:
            raise web.HTTPNotFound()
        now = time.monotonic()
        live = ctl.live_camera.source if ctl.live_camera else None
        return web.json_response(
            {
                "live_input": self.level(live, now),
                "clip_input": self.level(ctl.active_clip_input, now),
                "clip_on_air": ctl.active_clip_input is not None,
                "program_audio_muted": ctl.program_audio_muted,
                "clip_audio_muted": ctl.clip_audio_muted,
            }
        )
