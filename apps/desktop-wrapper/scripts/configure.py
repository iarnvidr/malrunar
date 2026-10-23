#!/usr/bin/env python3
"""Validate the wrapper configuration and write what the build needs (standard library only).

    python3 scripts/configure.py [--config FILE] [--check] [--skip-icons]

The configuration is a JSON file (default `wrapper.config.json`, else `wrapper.config.example.json`)
and individual values can be overridden from the environment (WRAPPER_NAME, WRAPPER_ID, WRAPPER_URL,
WRAPPER_VERSION, WRAPPER_ICON, WRAPPER_IDENTIFIER_PREFIX). `WRAPPER_CONFIG` names the file.
Anything invalid stops with a message that names the setting.

It writes, under src-tauri/:
  generated/app.json            the normalised settings the app reads at run time
  generated/tauri.overlay.json  what differs from tauri.conf.json: name, version, bundle id, icons
  generated/icons/              icons made from `icon` (only when one is configured)
  Info.plist                    the usage descriptions for the camera and microphone
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

APP_DIR = Path(__file__).resolve().parent.parent
TAURI_DIR = APP_DIR / "src-tauri"
DEFAULT_PREFIX = "io.github.iarnvidr"

KNOWN_KEYS = {"name", "id", "identifier_prefix", "url", "version", "icon", "internal_hosts", "window",
              "notifications", "hide_on_close"}
WINDOW_KEYS = {"width", "height", "min_width", "min_height"}
WINDOW_DEFAULTS = {"width": 1280, "height": 860, "min_width": 480, "min_height": 600}
ENV_OVERRIDES = {
    "WRAPPER_NAME": "name",
    "WRAPPER_ID": "id",
    "WRAPPER_IDENTIFIER_PREFIX": "identifier_prefix",
    "WRAPPER_URL": "url",
    "WRAPPER_VERSION": "version",
    "WRAPPER_ICON": "icon",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,39}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")
SEGMENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$|^[a-z]$")
LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}
ICON_FILES = ["32x32.png", "128x128.png", "128x128@2x.png", "icon.png", "icon.icns", "icon.ico"]


class ConfigError(Exception):
    """A setting is invalid. The message names it."""


def _string(config, key, default=None):
    value = config.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key}: must be a string")
    return value


def _int(window, key, low, high):
    value = window.get(key, WINDOW_DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"window.{key}: must be a whole number from {low} to {high}")
    return value


def _bool(config, key):
    value = config.get(key, True)
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: must be true or false")
    return value


def validate_url(value):
    """https, or http on a loopback host (for testing); no credentials, no spaces."""
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ConfigError("url: must be a non-empty address without spaces")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ConfigError(f"url: not a valid address ({error})") from None
    if not host:
        raise ConfigError("url: has no host")
    loopback = host in LOOPBACK or host == "::1"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ConfigError("url: must start with https:// (http:// is accepted only for localhost, 127.0.0.1 or [::1])")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("url: must not contain a user name or password")
    if not loopback and not (re.fullmatch(r"[0-9.]+", host) or ":" in host):
        if not all(LABEL_RE.match(label) for label in host.split(".")):
            raise ConfigError(f"url: {host!r} is not a valid host name (use the ASCII form of internationalised names)")
    return value if parts.path else value + "/"


def validate_host_pattern(value):
    if not isinstance(value, str):
        raise ConfigError("internal_hosts: every entry must be a string")
    pattern = value.lower()
    host = pattern[2:] if pattern.startswith("*.") else pattern
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(label) for label in labels):
        raise ConfigError(f"internal_hosts: {value!r} is not a host name (like login.example.com) or a *.example.com pattern")
    if re.fullmatch(r"[0-9.]+", host):
        raise ConfigError(f"internal_hosts: {value!r} is an IP address; use a host name")
    return pattern


def png_size(path):
    with open(path, "rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ConfigError(f"icon: {path} is not a PNG file")
    return struct.unpack(">II", header[16:24])


def validate_icon(value, base):
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    if not path.is_file():
        raise ConfigError(f"icon: {path} does not exist")
    suffix = path.suffix.lower()
    if suffix == ".png":
        width, height = png_size(path)
        if width != height or width < 512:
            raise ConfigError(f"icon: a PNG icon must be square and at least 512 x 512 pixels (this one is {width} x {height})")
    elif suffix != ".svg":
        raise ConfigError("icon: must be a .png (square, 1024 x 1024 recommended) or a .svg file")
    return path


def normalise(raw, base_dir, env=None):
    """The validated settings as a dict, plus the icon path (or None). `raw` is the parsed file."""
    env = os.environ if env is None else env
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a JSON object")
    raw = dict(raw)
    for variable, key in ENV_OVERRIDES.items():
        if env.get(variable):
            raw[key] = env[variable]
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)} (known: {', '.join(sorted(KNOWN_KEYS))})")
    for required in ("name", "id", "url"):
        if required not in raw:
            raise ConfigError(f"{required}: required")

    name = _string(raw, "name").strip()
    if not NAME_RE.match(name):
        raise ConfigError("name: 1 to 40 characters: letters, digits, spaces and . _ ' -, starting with a letter or digit")
    app_id = _string(raw, "id")
    if not ID_RE.match(app_id):
        raise ConfigError("id: 2 to 40 characters: lower case letters, digits and hyphens, starting with a letter, not ending with a hyphen")
    prefix = _string(raw, "identifier_prefix", DEFAULT_PREFIX)
    segments = prefix.split(".")
    if len(segments) < 2 or not all(SEGMENT_RE.match(s) for s in segments):
        raise ConfigError("identifier_prefix: dotted lower case segments such as io.github.yourname")
    identifier = f"{prefix}.{app_id}"
    if identifier.endswith(".app") or len(identifier) > 120:
        raise ConfigError("id: the bundle identifier must not end in .app or exceed 120 characters")
    version = _string(raw, "version", "0.1.0")
    if not VERSION_RE.match(version):
        raise ConfigError("version: must look like 1.2.3")
    url = validate_url(_string(raw, "url"))

    hosts = raw.get("internal_hosts", [])
    if not isinstance(hosts, list):
        raise ConfigError("internal_hosts: must be a list")
    internal_hosts = sorted({validate_host_pattern(h) for h in hosts})

    window = raw.get("window", {})
    if not isinstance(window, dict):
        raise ConfigError("window: must be an object")
    unknown = sorted(set(window) - WINDOW_KEYS)
    if unknown:
        raise ConfigError(f"window: unknown setting(s): {', '.join(unknown)}")
    window = {
        "width": _int(window, "width", 320, 10000),
        "height": _int(window, "height", 240, 10000),
        "min_width": _int(window, "min_width", 200, 10000),
        "min_height": _int(window, "min_height", 200, 10000),
    }
    if window["min_width"] > window["width"] or window["min_height"] > window["height"]:
        raise ConfigError("window: min_width and min_height must not exceed width and height")

    icon = None
    if raw.get("icon"):
        icon = validate_icon(_string(raw, "icon"), base_dir)

    app = {
        "name": name,
        "id": app_id,
        "identifier": identifier,
        "version": version,
        "url": url,
        "internal_hosts": internal_hosts,
        "window": window,
        "notifications": _bool(raw, "notifications"),
        "hide_on_close": _bool(raw, "hide_on_close"),
    }
    return app, icon


def tauri_overlay(app, icons):
    overlay = {
        "productName": app["name"],
        "version": app["version"],
        "identifier": app["identifier"],
        "bundle": {
            "shortDescription": f"{app['name']} desktop app",
            "longDescription": f"A desktop window for {app['url']}",
        },
    }
    if icons:
        overlay["bundle"]["icon"] = [f"generated/icons/{name}" for name in ICON_FILES]
    return overlay


def info_plist(app):
    name = escape(app["name"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>NSCameraUsageDescription</key><string>{name} uses the camera when a page you open asks for it.</string>
  <key>NSMicrophoneUsageDescription</key><string>{name} uses the microphone when a page you open asks for it.</string>
</dict>
</plist>
"""


def make_icons(icon, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["npx", "--no-install", "tauri", "icon", str(icon), "--output", str(out_dir)], cwd=APP_DIR, check=True)
    missing = [name for name in ICON_FILES if not (out_dir / name).is_file()]
    if missing:
        raise ConfigError(f"tauri icon did not produce {', '.join(missing)}")


def find_config(argument):
    if argument:
        return Path(argument)
    if os.environ.get("WRAPPER_CONFIG"):
        return Path(os.environ["WRAPPER_CONFIG"])
    for candidate in ("wrapper.config.json", "wrapper.config.example.json"):
        if (APP_DIR / candidate).is_file():
            return APP_DIR / candidate
    raise ConfigError("no configuration: create wrapper.config.json (see wrapper.config.example.json)")


def write_outputs(app, icon, tauri_dir=TAURI_DIR, skip_icons=False):
    generated = tauri_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    icons = False
    if icon is not None and not skip_icons:
        make_icons(icon, generated / "icons")
        icons = True
    (generated / "app.json").write_text(json.dumps(app, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    overlay = tauri_overlay(app, icons)
    (generated / "tauri.overlay.json").write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
    (tauri_dir / "Info.plist").write_text(info_plist(app), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", help="the configuration file")
    parser.add_argument("--check", action="store_true", help="only validate and print the result")
    parser.add_argument("--skip-icons", action="store_true", help="do not generate icons from `icon`")
    args = parser.parse_args(argv)
    try:
        path = find_config(args.config)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigError(f"cannot read {path}: {error}") from None
        app, icon = normalise(raw, path.resolve().parent)
        if args.check:
            print(json.dumps(app, indent=2, sort_keys=True))
            return 0
        write_outputs(app, icon, skip_icons=args.skip_icons)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    print(f"configured {app['name']!r} ({app['identifier']}) for {app['url']} from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
