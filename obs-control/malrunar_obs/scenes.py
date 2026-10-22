# SPDX-License-Identifier: Apache-2.0
"""Generate the OBS profile, scene collection and obs-websocket settings from the configuration.

Everything OBS needs is written before OBS starts, so a fresh container never shows the
first-run wizard and the scene names, sources and output are exactly what the config says.
Plugins can add scenes and sources through ``extend_collection(builder, config)``.
"""
from __future__ import annotations

import configparser
import copy
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .config import Config

_UUID_NAMESPACE = uuid.UUID("6f1b0d2e-5c0a-4d58-9a43-2f0e6a7c9b11")
# The version marker OBS 32 writes into sources; older markers are upgraded by OBS on load.
PREV_VER = 520159234
# libavcodec ids OBS stores in the FFmpeg output settings.
FF_VIDEO_ENCODER_ID = "27"  # AV_CODEC_ID_H264
FF_AUDIO_ENCODER_ID = "86018"  # AV_CODEC_ID_AAC


def color_value(css_color: str) -> int:
    """'#rrggbb' -> OBS colour integer (0xAABBGGRR, opaque)."""
    text = css_color.strip().lstrip("#")
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"colour {css_color!r} must look like #rrggbb")
    red, green, blue = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return 0xFF000000 | (blue << 16) | (green << 8) | red


def write_ini(path: Path, sections: dict[str, dict[str, str]], *, preserve: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    if preserve:
        parser.read(path, encoding="utf-8")
    for section, values in sections.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in values.items():
            parser.set(section, key, str(value))
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)


def source_base(name: str, source_uuid: str, source_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "prev_ver": PREV_VER,
        "name": name,
        "uuid": source_uuid,
        "id": source_id,
        "versioned_id": source_id,
        "settings": settings,
        "mixers": 255,
        "sync": 0,
        "flags": 0,
        "volume": 1.0,
        "balance": 0.5,
        "enabled": True,
        "muted": False,
        "push-to-mute": False,
        "push-to-mute-delay": 0,
        "push-to-talk": False,
        "push-to-talk-delay": 0,
        "hotkeys": {},
        "deinterlace_mode": 0,
        "deinterlace_field_order": 0,
        "monitoring_type": 0,
        "private_settings": {},
    }


def scene_item(name: str, source_uuid: str, item_id: int, bounds: tuple[float, float]) -> dict[str, Any]:
    return {
        "name": name,
        "source_uuid": source_uuid,
        "visible": True,
        "locked": True,
        "rot": 0.0,
        "pos": {"x": 0.0, "y": 0.0},
        "scale": {"x": 1.0, "y": 1.0},
        "align": 5,
        "bounds_type": 2,
        "bounds_align": 0,
        "bounds": {"x": float(bounds[0]), "y": float(bounds[1])},
        "crop_left": 0,
        "crop_top": 0,
        "crop_right": 0,
        "crop_bottom": 0,
        "id": item_id,
        "group_item_backup": False,
        "scale_filter": "bicubic",
        "blend_method": "default",
        "blend_type": "normal",
        "show_transition": {"duration": 0},
        "hide_transition": {"duration": 0},
        "private_settings": {},
    }


class CollectionBuilder:
    """The scene collection under construction; also what plugins extend.

    ``add_source`` registers an input, ``add_scene`` a scene whose items are existing sources (in
    back-to-front order). ``profile_ini`` holds extra profile settings (``{section: {key: value}}``,
    merged over the generated ``basic.ini``) and ``profile_files`` extra files for the profile
    directory (``{filename: text}``).
    """

    def __init__(self, config: Config):
        self.config = config
        self.canvas = (config.video.width, config.video.height)
        self.sources: list[dict[str, Any]] = []
        self.scene_order: list[str] = []
        self.profile_ini: dict[str, dict[str, str]] = {}
        self.profile_files: dict[str, str] = {}
        self.collection_fields: dict[str, Any] = {}
        self._uuids: dict[str, str] = {}

    def uuid_of(self, name: str) -> str:
        if name not in self._uuids:
            seed = f"{self.config.obs.collection}/{name}"
            self._uuids[name] = str(uuid.uuid5(_UUID_NAMESPACE, seed))
        return self._uuids[name]

    def has(self, name: str) -> bool:
        return any(source["name"] == name for source in self.sources)

    def add_source(self, name: str, source_id: str, settings: dict[str, Any], **fields: Any) -> dict[str, Any]:
        if self.has(name):
            raise ValueError(f"OBS source or scene {name!r} already exists in the collection")
        source = source_base(name, self.uuid_of(name), source_id, settings)
        source.update(fields)
        self.sources.append(source)
        return source

    def add_scene(self, name: str, items: Iterable[str | tuple[str, tuple[float, float]]], *, order: bool = True) -> dict[str, Any]:
        entries = []
        for index, item in enumerate(items, 1):
            child, bounds = (item, self.canvas) if isinstance(item, str) else item
            if not self.has(child):
                raise ValueError(f"scene {name!r}: source {child!r} is not defined yet")
            entries.append(scene_item(child, self.uuid_of(child), index, bounds))
        scene = self.add_source(
            name, "scene", {"id_counter": len(entries), "custom_size": False, "items": entries}, mixers=0
        )
        if order:
            self.scene_order.append(name)
        return scene

    def as_collection(self, start_scene: str) -> dict[str, Any]:
        collection = {
            "current_scene": start_scene,
            "current_program_scene": start_scene,
            "scene_order": [{"name": name} for name in self.scene_order],
            "name": self.config.obs.collection,
            "sources": self.sources,
            "groups": [],
            "quick_transitions": [{"name": "Cut", "duration": 0, "hotkeys": [], "id": 1, "fade_to_black": False}],
            "transitions": [],
            "saved_projectors": [],
            "current_transition": "Cut",
            "transition_duration": 0,
            "preview_locked": True,
            "scaling_enabled": False,
            "scaling_level": 0,
            "scaling_off_x": 0.0,
            "scaling_off_y": 0.0,
            "modules": {},
        }
        collection.update(self.collection_fields)
        return collection


def build_collection(config: Config, plugins: Iterable[Any] = ()) -> CollectionBuilder:
    """The generic scenes: one per camera, the b-roll/media scene and the stopped scene."""
    builder = CollectionBuilder(config)
    width, height = builder.canvas
    fps = config.video.fps
    scenes: list[tuple[str, str]] = []  # (scene, source) in order
    for camera in config.cameras:
        url = config.camera_url(camera)
        settings = {
            "url": url,
            "width": camera.width or width,
            "height": camera.height or height,
            "fps": camera.fps or fps,
            "shutdown": camera.shutdown_when_hidden,
            "restart_when_active": (
                camera.restart_when_active
                if camera.restart_when_active is not None
                else not config.features.program_control
            ),
            "reroute_audio": camera.reroute_audio,
            "is_local_file": False,
            "css": camera.css,
        }
        if url.startswith("file://"):  # a page on the container's disk
            settings.update({"is_local_file": True, "local_file": unquote(urlsplit(url).path), "url": ""})
        builder.add_source(camera.source, "browser_source", settings)
        scenes.append((camera.scene, camera.source))
    media = config.media
    builder.add_source(
        media.source,
        "ffmpeg_source",
        {
            "local_file": media.file,
            "looping": True,
            "restart_on_activate": True,
            "close_when_inactive": False,
            "clear_on_media_end": False,
            "hw_decode": False,
        },
    )
    scenes.append((media.scene, media.source))
    stopped = config.stopped
    builder.add_source(
        stopped.source,
        "color_source_v3",
        {"color": color_value(stopped.color), "width": width, "height": height},
    )
    stopped_entry = (stopped.scene, stopped.source)
    ordered = [stopped_entry] + scenes if config.features.program_control else scenes + [stopped_entry]
    for scene_name, source_name in ordered:
        builder.add_scene(scene_name, [source_name])
    for plugin in plugins:
        hook = getattr(plugin, "extend_collection", None)
        if hook:
            hook(builder, config)
    return builder


def profile_sections(config: Config) -> dict[str, dict[str, str]]:
    video, output = config.video, config.output
    advanced = {
        "RecType": "FFmpeg",
        "FFOutputToFile": "false",
        "FFURL": output.url,
        "FFFormat": "mpegts",
        "FFFormatMimeType": "video/MP2T",
        "FFVEncoderId": FF_VIDEO_ENCODER_ID,
        "FFVEncoder": "libx264",
        "FFAEncoderId": FF_AUDIO_ENCODER_ID,
        "FFAEncoder": "aac",
        "FFVBitrate": str(output.video_bitrate_kbps),
        "FFVGOPSize": str(output.gop),
        "FFVCustom": (
            f"preset={output.x264_preset} tune=zerolatency "
            f"x264-params=repeat-headers=1:keyint={output.gop}:scenecut=0"
        ),
        "FFABitrate": str(output.audio_bitrate_kbps),
        "FFAudioMixes": "1",
        "FFUseRescale": "false",
        "FFIgnoreCompat": "true",
        "FFMCustom": "mpegts_flags=+resend_headers",
    }
    if output.rescale:
        advanced.update({"FFRescale": "true", "FFRescaleRes": output.rescale})
    return {
        "General": {"Name": config.obs.profile},
        "Video": {
            "BaseCX": str(video.width),
            "BaseCY": str(video.height),
            "OutputCX": str(video.width),
            "OutputCY": str(video.height),
            "FPSType": "1",
            "FPSCommon": str(video.fps),
            "FPSInt": str(video.fps),
            "FPSNum": str(video.fps),
            "FPSDen": "1",
            "ScaleType": "bicubic",
            "ColorFormat": "NV12",
            "ColorSpace": "709",
            "ColorRange": "Partial",
        },
        "Audio": {
            "MonitoringDeviceId": "default",
            "MonitoringDeviceName": "Default",
            "SampleRate": "48000",
            "ChannelSetup": "Stereo",
        },
        "Output": {"Mode": "Advanced"},
        "AdvOut": advanced,
    }


def websocket_config_path(config: Config) -> Path:
    return Path(config.obs.config_dir) / "plugin_config" / "obs-websocket" / "config.json"


def stored_websocket_password(config: Config) -> str:
    try:
        return str(json.loads(websocket_config_path(config).read_text(encoding="utf-8")).get("server_password", ""))
    except (OSError, ValueError):
        return ""


def websocket_password(config: Config) -> str:
    """The obs-websocket password the controller must use ('' when authentication is off)."""
    if not config.obs.websocket_auth:
        return ""
    return config.obs.websocket_password or stored_websocket_password(config)


def generate(config: Config, plugins: Iterable[Any] = ()) -> CollectionBuilder:
    """Write global.ini, user.ini, the profile, the scene collection and obs-websocket settings."""
    root = Path(config.obs.config_dir)
    profile, collection_name = config.obs.profile, config.obs.collection
    basic = {
        "Profile": profile,
        "ProfileDir": profile,
        "SceneCollection": collection_name,
        "SceneCollectionFile": collection_name,
    }
    write_ini(
        root / "global.ini",
        {
            "General": {"FirstRun": "true", "EnableAutoUpdates": "false", "BrowserHWAccel": "false"},
            "Basic": {**basic, "ConfigOnNewProfile": "false"},
        },
    )
    # OBS 32 moved the onboarding state to user.ini. Seed both so a new container never blocks
    # automated output behind the configuration wizard, and keep the user's other preferences.
    write_ini(
        root / "user.ini",
        {"General": {"FirstRun": "true"}, "Basic": {**basic, "ConfigOnNewProfile": "false"}},
        preserve=True,
    )

    builder = build_collection(config, plugins)
    sections = copy.deepcopy(profile_sections(config))
    for section, values in builder.profile_ini.items():
        sections.setdefault(section, {}).update({k: str(v) for k, v in values.items()})
    profile_dir = root / "basic" / "profiles" / profile
    write_ini(profile_dir / "basic.ini", sections)
    for filename, text in builder.profile_files.items():
        if "/" in filename or filename.startswith("."):
            raise ValueError(f"profile file name {filename!r} must be a plain file name")
        (profile_dir / filename).write_text(text, encoding="utf-8")

    password = ""
    if config.obs.websocket_auth:
        password = config.obs.websocket_password or stored_websocket_password(config) or secrets.token_urlsafe(24)
    ws_path = websocket_config_path(config)
    ws_path.parent.mkdir(parents=True, exist_ok=True)
    ws_path.write_text(
        json.dumps(
            {
                "alerts_enabled": False,
                "auth_required": bool(config.obs.websocket_auth),
                "first_load": False,
                "server_enabled": True,
                "server_password": password,
                "server_port": config.obs.websocket_port,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(ws_path, 0o600)

    scene_file = root / "basic" / "scenes" / f"{collection_name}.json"
    scene_file.parent.mkdir(parents=True, exist_ok=True)
    scene_file.write_text(json.dumps(builder.as_collection(config.start_scene_name), indent=2), encoding="utf-8")
    return builder


def launch_args(config: Config) -> list[str]:
    """Arguments for the obs binary."""
    args = [
        "--disable-updater",
        "--disable-shutdown-check",
        "--disable-missing-files-check",
        "--multi",
    ]
    if config.output.autostart:
        args.append("--startrecording")
    args += [
        "--profile", config.obs.profile,
        "--collection", config.obs.collection,
        "--scene", config.start_scene_name,
    ]
    return args
