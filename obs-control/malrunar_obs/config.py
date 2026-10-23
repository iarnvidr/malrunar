# SPDX-License-Identifier: Apache-2.0
"""Configuration of obs-control: defaults, an optional TOML/JSON file, environment overrides.

Precedence, lowest to highest: built-in defaults, the config file, MALRUNAR_OBS_* environment
variables. See docs/obs-control-api.md for the complete reference.

The file is TOML (``.toml``) or JSON (``.json``), chosen by extension. Its location is
``MALRUNAR_OBS_CONFIG``; without it, ``/etc/malrunar/obs-control.toml`` is read if it exists.

Every scalar setting ``[section] key`` can be set with ``MALRUNAR_OBS_<SECTION>_<KEY>`` (for
``[obs]``: ``MALRUNAR_OBS_<KEY>``). The first
camera (``[[cameras]]`` entry 0) is set with ``MALRUNAR_OBS_CAMERA_<KEY>``. A few short aliases
exist (``ENV_ALIASES``).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .forward_auth import ConfigError as IdentityConfigError
from .forward_auth import IdentityConfig

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

ENV_PREFIX = "MALRUNAR_OBS_"
DEFAULT_CONFIG_PATH = "/etc/malrunar/obs-control.toml"

CAMERA_DEFAULTS: dict[str, Any] = {
    "key": "live",
    "scene": "Live Feed",
    "source": "Camera Receiver",
    "url": "",
    "url_file": "",
    "width": 0,
    "height": 0,
    "fps": 0,
    "css": "body { margin: 0; background: #000; overflow: hidden; }",
    "restart_when_active": None,  # None: true, except false with features.program_control (keeps a peer connection alive)
    "reroute_audio": True,
    "shutdown_when_hidden": False,
}

DEFAULTS: dict[str, Any] = {
    "obs": {
        "config_dir": "",  # empty: $XDG_CONFIG_HOME/obs-studio, else $HOME/.config/obs-studio
        "profile": "malrunar",
        "collection": "malrunar",
        "websocket_port": 4455,
        "websocket_password": "",
        "websocket_auth": True,
        "start_scene": "",
    },
    "http": {"bind": "0.0.0.0", "port": 18095},
    "video": {"width": 640, "height": 360, "fps": 24, "default_profile": "360p"},
    "output": {
        "url": "udp://127.0.0.1:18096?pkt_size=1316",
        "video_bitrate_kbps": 1200,
        "audio_bitrate_kbps": 128,
        "gop": 48,
        "x264_preset": "veryfast",
        "rescale": "",
        "autostart": True,
    },
    "cameras": [dict(CAMERA_DEFAULTS)],
    "media": {"key": "video", "scene": "B-roll Video", "source": "B-roll Source", "file": ""},
    "stopped": {"scene": "Stopped", "source": "Stopped Frame", "color": "#000000"},
    "clips": {"scene_prefix": "Recorded", "source_prefix": "Recorded Source"},
    "features": {
        "program_control": False,
        "video_profiles": False,
        "sender_lease": True,
        "mode_label": "program-control",
        "takes": False,
    },
    "sender": {"lease_seconds": 12.0},
    "broll": {
        "enabled": True,
        "auto": True,
        "seconds": 180,
        "chunk_seconds": 90,
        "max_seconds": 1200,
        "silence_db": -38.0,
        "motion_threshold": 0.025,
        "min_segment_seconds": 6.0,
        "preview_url": "",
    },
    "access": {
        "identity_header": "",  # the user header; empty: Remote-User
        "groups_header": "",  # empty: Remote-Groups
        "name_header": "",  # empty: Remote-Name
        "email_header": "",  # empty: Remote-Email
        "trusted_proxies": [],
        "proxy_secret_header": "",
        "proxy_secret_file": "",
        "groups": {},  # capability -> list of groups, e.g. operator = ["calls-operators"]
    },
    "storage": {"recordings_dir": "/config/recordings", "state_dir": "/config/state"},
    "relay": {
        "control_url": "http://127.0.0.1:18097",
        "bind_host": "127.0.0.1",
        "port": 18096,
        "target_host": "",
        "target_port": 18096,
        "control_host": "",
        "control_port": 18097,
        "capture_dir": "/tmp/malrunar-obs-captures",
    },
    "console": {"enabled": True, "title": "OBS control", "sender_url": "", "secure_url": ""},
    "profiles": {
        "360p": {"width": 640, "height": 360, "encoder_kbps": 1200, "camera_query": {}, "camera_query_drop": []},
        "540p": {"width": 960, "height": 540, "encoder_kbps": 2000, "camera_query": {}, "camera_query_drop": []},
        "720p": {"width": 1280, "height": 720, "encoder_kbps": 3000, "camera_query": {}, "camera_query_drop": []},
    },
    "plugins": {"modules": [], "dir": ""},
    "plugin": {},
}

PROFILE_DEFAULTS = {"width": 640, "height": 360, "encoder_kbps": 1200, "camera_query": {}, "camera_query_drop": []}

# Short environment names kept for the settings people set most often.
ENV_ALIASES = {
    "MALRUNAR_OBS_CONTROL_PORT": ("http", "port"),
    "MALRUNAR_OBS_CONTROL_BIND": ("http", "bind"),
    "MALRUNAR_OBS_RECORDINGS_DIR": ("storage", "recordings_dir"),
    "MALRUNAR_OBS_STATE_DIR": ("storage", "state_dir"),
    "MALRUNAR_OBS_MEDIA_FILE": ("media", "file"),
    "MALRUNAR_OBS_PLUGINS": ("plugins", "modules"),
    "MALRUNAR_OBS_PLUGINS_DIR": ("plugins", "dir"),
}

# Settings read from the environment only: JSON object of controller state to resume after a
# controller-only restart (see docs/obs-control-api.md), and the config file location.
ENV_CONFIG = "MALRUNAR_OBS_CONFIG"
ENV_RESTORE = "MALRUNAR_OBS_RESTORE"

CAPABILITIES = ("viewer", "operator")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """The configuration is invalid; the message names the setting and where to change it."""


class Section:
    """Read-only attribute view of a dict."""

    def __init__(self, data: Mapping[str, Any]):
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("configuration is read-only")

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:
        return f"Section({self._data!r})"


class Config:
    """Validated configuration. ``config.output.url``, ``config.cameras[0].source``, ..."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        for name, value in data.items():
            if name == "cameras":
                setattr_obj(self, name, [Section(item) for item in value])
            elif isinstance(value, dict) and name not in ("profiles", "plugin"):
                setattr_obj(self, name, Section(value))
            else:
                setattr_obj(self, name, value)

    # -- derived values ---------------------------------------------------------------------
    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.obs.websocket_port}"

    def camera(self, key: str) -> Section | None:
        return next((camera for camera in self.cameras if camera.key == key), None)

    def camera_url(self, camera: Section) -> str:
        """The browser-source URL of a camera; ``url_file`` is re-read on every call."""
        if camera.url_file:
            return Path(camera.url_file).read_text(encoding="utf-8").strip()
        return camera.url

    @property
    def scene_keys(self) -> dict[str, str]:
        """Scene key (as used by POST /api/scene/<key>) -> OBS scene name."""
        keys = {camera.key: camera.scene for camera in self.cameras}
        keys[self.media.key] = self.media.scene
        return keys

    @property
    def primary_camera(self) -> Section | None:
        return self.cameras[0] if self.cameras else None

    @property
    def start_scene_name(self) -> str:
        wanted = self.obs.start_scene
        names = self.scene_keys
        if wanted:
            if wanted == "stopped":
                return self.stopped.scene
            return names.get(wanted, wanted)
        if self.features.program_control or not self.cameras:
            return self.stopped.scene
        return self.cameras[0].scene

    @property
    def trust_configured(self) -> bool:
        """Whether a trusted proxy address or a proxy secret is configured (identity headers can be trusted)."""
        access = self.access
        return bool(access.trusted_proxies or access.proxy_secret_header)

    @property
    def identity_active(self) -> bool:
        """Whether identities are in use at all: a user header or any trust setting is configured."""
        return bool(self.access.identity_header) or self.trust_configured

    @property
    def per_identity_broll(self) -> bool:
        return self.identity_active

    def identity_config(self) -> IdentityConfig:
        """The forward-auth settings (reads the proxy secret file); raises ConfigError."""
        try:
            return IdentityConfig.from_mapping(_identity_values(self.access.to_dict()), "[access] ")
        except IdentityConfigError as exc:
            raise ConfigError(f"[access]: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)


def _identity_values(access: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "USER_HEADER": access["identity_header"],
        "GROUPS_HEADER": access["groups_header"],
        "NAME_HEADER": access["name_header"],
        "EMAIL_HEADER": access["email_header"],
        "TRUSTED_PROXIES": access["trusted_proxies"],
        "PROXY_SECRET_HEADER": access["proxy_secret_header"],
        "PROXY_SECRET_FILE": access["proxy_secret_file"],
    }


def setattr_obj(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


# -- loading ----------------------------------------------------------------------------------
def _read_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file {path} not found (set by {ENV_CONFIG})")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".toml":
            if tomllib is None:  # pragma: no cover
                raise ConfigError("TOML needs Python 3.11 or newer; use a .json config file")
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            raise ConfigError(f"config file {path}: use a .toml or .json extension")
    except PermissionError as exc:
        raise ConfigError(f"config file {path}: permission denied (cannot read configuration)") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path}: the top level must be a table/object")
    return data


def _coerce(value: str, like: Any, where: str) -> Any:
    try:
        if isinstance(like, bool) or like is None:
            lowered = value.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError("expected true or false")
        if isinstance(like, int):
            return int(value)
        if isinstance(like, float):
            return float(value)
        if isinstance(like, list):
            return [item.strip() for item in value.replace(":", ",").split(",") if item.strip()]
        return value
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc} (got {value!r})") from exc


def _check_type(value: Any, like: Any, where: str) -> Any:
    if like is None:  # an optional boolean: unset, true or false
        if value is not None and not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false")
        return value
    if isinstance(like, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false")
        return value
    if isinstance(like, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer")
        return value
    if isinstance(like, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number")
        return float(value)
    if isinstance(like, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string")
        return value
    if isinstance(like, list):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{where}: expected a list of strings")
        return list(value)
    return value


def _merge_section(name: str, base: dict[str, Any], given: Any) -> dict[str, Any]:
    if not isinstance(given, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(given) - set(base)
    if unknown:
        raise ConfigError(f"[{name}]: unknown setting(s) {', '.join(sorted(unknown))}")
    merged = dict(base)
    for key, value in given.items():
        if name == "access" and key == "groups":
            merged[key] = _check_groups(value)
        else:
            merged[key] = _check_type(value, base[key], f"[{name}] {key}")
    return merged


def _check_groups(value: Any) -> dict[str, list[str]]:
    where = "[access.groups]"
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table of capability = [groups]")
    unknown = set(value) - set(CAPABILITIES)
    if unknown:
        raise ConfigError(f"{where}: unknown capability {', '.join(sorted(unknown))} (use {', '.join(CAPABILITIES)})")
    result = {}
    for capability, groups in value.items():
        if not isinstance(groups, list) or not all(isinstance(g, str) and g.strip() for g in groups):
            raise ConfigError(f"{where} {capability}: expected a list of group names")
        result[capability] = [g.strip() for g in groups]
    return result


def _merge_profiles(given: Any) -> dict[str, Any]:
    if not isinstance(given, dict) or not given:
        raise ConfigError("[profiles] must be a table of profile tables (or omitted)")
    profiles = {}
    for name, value in given.items():
        if not isinstance(value, dict):
            raise ConfigError(f"[profiles.{name}] must be a table")
        merged = _merge_section(f"profiles.{name}", PROFILE_DEFAULTS, value)
        if not isinstance(merged["camera_query"], dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in merged["camera_query"].items()
        ):
            raise ConfigError(f"[profiles.{name}] camera_query must be a table of strings")
        merged["camera_query"] = {k: str(v) for k, v in merged["camera_query"].items()}
        if not isinstance(merged["camera_query_drop"], list):
            raise ConfigError(f"[profiles.{name}] camera_query_drop must be a list of strings")
        if merged["width"] <= 0 or merged["height"] <= 0:
            raise ConfigError(f"[profiles.{name}] width and height must be positive")
        profiles[name] = merged
    return profiles


def merge_file_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Defaults + the contents of a config file (already parsed)."""
    result = copy.deepcopy(DEFAULTS)
    top_unknown = set(data) - set(DEFAULTS)
    if top_unknown:
        raise ConfigError(f"unknown section(s) {', '.join(sorted(top_unknown))}; plugin settings go under [plugin.<name>]")
    for name, value in data.items():
        if name == "cameras":
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ConfigError("[[cameras]] must be an array of tables")
            cameras = []
            for index, item in enumerate(value):
                cameras.append(_merge_section(f"cameras[{index}]", CAMERA_DEFAULTS, item))
            result["cameras"] = cameras
        elif name == "profiles":
            result["profiles"] = _merge_profiles(value)
        elif name == "plugin":
            if not isinstance(value, dict):
                raise ConfigError("[plugin] must contain one table per plugin")
            result["plugin"] = copy.deepcopy(value)
        else:
            result[name] = _merge_section(name, DEFAULTS[name], value)
    return result


def env_name(section: str, key: str) -> str:
    """MALRUNAR_OBS_<SECTION>_<KEY>; the [obs] section needs no extra prefix (MALRUNAR_OBS_PROFILE)."""
    return f"{ENV_PREFIX}{key}".upper() if section == "obs" else f"{ENV_PREFIX}{section}_{key}".upper()


def apply_env(data: dict[str, Any], env: Mapping[str, str]) -> None:
    """Overlay MALRUNAR_OBS_* variables onto the merged data (in place)."""
    claimed: set[str] = set()
    for alias, (section, key) in ENV_ALIASES.items():
        if alias in env:
            claimed.add(alias)
            data[section][key] = _coerce(env[alias], DEFAULTS[section][key], alias)
    for section, values in DEFAULTS.items():
        if section in ("cameras", "profiles", "plugin") or not isinstance(values, dict):
            continue
        for key, like in values.items():
            if section == "access" and key == "groups":
                continue
            name = env_name(section, key)
            if name in env and name not in claimed:
                data[section][key] = _coerce(env[name], like, name)
    # [access.groups]: MALRUNAR_OBS_ACCESS_GROUPS_<CAPABILITY> = comma-separated groups.
    for capability in CAPABILITIES:
        name = f"{ENV_PREFIX}ACCESS_GROUPS_{capability}".upper()
        if name in env:
            groups = _coerce(env[name], [], name)
            if groups:
                data["access"]["groups"] = {**data["access"]["groups"], capability: groups}
            else:
                data["access"]["groups"] = {k: v for k, v in data["access"]["groups"].items() if k != capability}
    # The first camera: MALRUNAR_OBS_CAMERA_<KEY>.
    overrides = {
        key: _coerce(env[f"{ENV_PREFIX}CAMERA_{key}".upper()], like, f"{ENV_PREFIX}CAMERA_{key}".upper())
        for key, like in CAMERA_DEFAULTS.items()
        if f"{ENV_PREFIX}CAMERA_{key}".upper() in env
    }
    if overrides:
        if not data["cameras"]:
            data["cameras"] = [dict(CAMERA_DEFAULTS)]
        data["cameras"][0].update(overrides)


def validate(data: dict[str, Any], *, cameras: bool = True) -> None:
    keys: dict[str, str] = {}
    scenes: dict[str, str] = {}

    def claim(kind: str, key: str, scene: str, source: str) -> None:
        if not key or not key.replace("-", "").replace("_", "").isalnum():
            raise ConfigError(f"{kind}: key {key!r} must be letters, digits, '-' or '_'")
        if key in ("stopped",) or key in keys:
            raise ConfigError(f"{kind}: scene key {key!r} is already used")
        keys[key] = kind
        for label, name in (("scene", scene), ("source", source)):
            if not name.strip():
                raise ConfigError(f"{kind}: {label} name must not be empty")
            if name in scenes:
                raise ConfigError(f"{kind}: name {name!r} is already used by {scenes[name]}")
            scenes[name] = f"{kind} {label}"

    for index, camera in enumerate(data["cameras"]):
        if cameras and not (camera["url"] or camera["url_file"]):
            raise ConfigError(
                f"cameras[{index}] ({camera['key']}): set a browser-source URL "
                f"({'MALRUNAR_OBS_CAMERA_URL' if index == 0 else '[[cameras]] url'})"
            )
        if camera["url"] and camera["url_file"]:
            raise ConfigError(f"cameras[{index}]: set url or url_file, not both")
        claim(f"cameras[{index}]", camera["key"], camera["scene"], camera["source"])
    media, stopped = data["media"], data["stopped"]
    claim("media", media["key"], media["scene"], media["source"])
    for label, name in (("stopped scene", stopped["scene"]), ("stopped source", stopped["source"])):
        if not name.strip() or name in scenes:
            raise ConfigError(f"{label}: {name!r} is empty or already used")
        scenes[name] = label
    if not data["profiles"]:
        raise ConfigError("[profiles] must define at least one profile")
    if data["video"]["default_profile"] not in data["profiles"]:
        raise ConfigError(f"[video] default_profile {data['video']['default_profile']!r} is not defined in [profiles]")
    video = data["video"]
    if video["width"] <= 0 or video["height"] <= 0 or video["fps"] <= 0:
        raise ConfigError("[video] width, height and fps must be positive")
    rescale = data["output"]["rescale"]
    if rescale and not (rescale.count("x") == 1 and all(part.isdigit() for part in rescale.split("x"))):
        raise ConfigError("[output] rescale must look like 640x360")
    color = data["stopped"]["color"]
    if not (len(color) == 7 and color[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in color[1:])):
        raise ConfigError(f"[stopped] color must look like #rrggbb (got {color!r})")
    if not data["output"]["url"]:
        raise ConfigError("[output] url is required (MALRUNAR_OBS_OUTPUT_URL)")
    start = data["obs"]["start_scene"]
    if start and start != "stopped" and start not in keys and start not in scenes:
        raise ConfigError(f"[obs] start_scene {start!r} is not a scene key or scene name")
    for name in ("recordings_dir", "state_dir"):
        if not data["storage"][name]:
            raise ConfigError(f"[storage] {name} must not be empty")
    access = data["access"]
    if bool(access["proxy_secret_header"]) != bool(access["proxy_secret_file"]):
        raise ConfigError("[access] proxy_secret_header and proxy_secret_file go together: set both or neither")
    try:  # header names and CIDRs (the secret file is read when the controller starts)
        IdentityConfig.from_mapping({**_identity_values(access), "PROXY_SECRET_HEADER": "", "PROXY_SECRET_FILE": ""}, "[access] ")
    except IdentityConfigError as exc:
        raise ConfigError(f"[access]: {exc}") from exc
    if access["groups"] and not (access["trusted_proxies"] or access["proxy_secret_header"]):
        raise ConfigError(
            "[access.groups] needs trust: set access.trusted_proxies and/or access.proxy_secret_header "
            "(otherwise no identity is trusted and every request would be refused)"
        )
    http_port = data["http"]["port"]
    if not 0 <= http_port <= 65535:
        raise ConfigError("[http] port must be between 0 and 65535")


def load_config(
    env: Mapping[str, str] | None = None, path: str | os.PathLike | None = None, *, require_cameras: bool = True
) -> Config:
    """Load defaults, the config file and the environment into a validated Config.

    ``require_cameras=False`` is for processes that do not use the cameras (the relay).
    """
    env = os.environ if env is None else env
    chosen = path or env.get(ENV_CONFIG) or ""
    file_data: dict[str, Any] = {}
    if chosen:
        file_data = _read_file(Path(chosen))
    elif Path(DEFAULT_CONFIG_PATH).is_file():
        file_data = _read_file(Path(DEFAULT_CONFIG_PATH))
    data = merge_file_data(file_data)
    apply_env(data, env)
    if not data["obs"]["config_dir"]:
        base = env.get("XDG_CONFIG_HOME") or (f"{env['HOME']}/.config" if env.get("HOME") else "/config/.config")
        data["obs"]["config_dir"] = f"{base}/obs-studio"
    validate(data, cameras=require_cameras)
    return Config(data)
