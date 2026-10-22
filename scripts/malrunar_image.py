#!/usr/bin/env python3
"""Content-addressed identity of the malrunar images (standard library only).

Every image has a hash that covers everything that can change it and can be
computed offline, without Docker:

  * the image's recipe: every file under <image>/ except test/ (Dockerfile,
    patches, scripts), each as path + exec bit + sha256;
  * the pins of the image: its own <image>/pins.env and those keys of the root pins.env that its
    Dockerfile declares as `ARG` (a root pin the image does not use cannot change it, so bumping
    it must not republish the image: media-bridge, which only copies scripts, uses none);
  * the hash of the parent image, if the image is built FROM another one of
    ours (obs-runtime is built FROM desktop-base, obs-control FROM obs-runtime), so a child is never paired
    with a different parent.

Tags published for an image: `<version>` (human, from <image>/VERSION or the
image's own VERSION pin) and `h-<hash>` (the content hash).

Usage:
  malrunar_image.py list
  malrunar_image.py hash <image>
  malrunar_image.py tags <image>          # one tag per line
  malrunar_image.py parent <image>        # parent image name or empty
  malrunar_image.py pins <image>          # the pins the image's build uses, as KEY=VALUE
  malrunar_image.py pins --all <image>    # every root pin plus the image's own (for tests)
  malrunar_image.py legacy-hash <image>   # the hash before pins were scoped (publish.yml retags once)
"""
import hashlib
import os
import re
import sys
from pathlib import Path

SCHEMA = 1
ROOT = Path(__file__).resolve().parent.parent
# image -> parent image built FROM (None = built from the pinned Ubuntu base or scratch)
IMAGES = {
    "desktop-base": None,
    "obs-runtime": "desktop-base",
    "obs-control": "obs-runtime",
    "guacd": None,
    "guacamole-tunnel": None,
    "tail-browser-mcp": None,
    "media-bridge": None,
    "office-convert": None,
    "node": None,
    "work-browser": None,
    "session-kit": None,
}
# Images whose smoke test runs them inside or next to other images. This is test-time only (it is
# not part of the hash and not a build parent); changed_images.py re-tests them when those change.
TESTED_WITH = {
    "media-bridge": ("desktop-base",),
    "work-browser": ("desktop-base",),
    "session-kit": ("desktop-base", "work-browser"),
}
# Files that do not influence image content.
EXCLUDED_DIRS = {"test", "__pycache__", "node_modules", ".git"}
# Pin that provides the human-readable version tag of each image.
VERSION_PIN = "VERSION"


def parse_pins(path):
    pins = {}
    if not path.exists():
        return pins
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise SystemExit(f"{path}:{number}: expected KEY=VALUE")
        pins[key.strip()] = value.strip()
    return pins


def declared_args(image, root=ROOT):
    """Names of the build args the image's Dockerfile declares (ARG NAME or ARG NAME=default)."""
    text = (root / image / "Dockerfile").read_text()
    return set(re.findall(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", text, flags=re.MULTILINE))


def merged_pins(image, root=ROOT, scoped=True):
    """Root pins (only those the Dockerfile declares, when scoped) plus the image's own pins."""
    pins = parse_pins(root / "pins.env")
    own = parse_pins(root / image / "pins.env")
    clash = set(pins) & set(own)
    if clash:
        raise SystemExit(f"{image}/pins.env redefines root pins: {sorted(clash)}")
    if scoped:
        used = declared_args(image, root)
        pins = {key: value for key, value in pins.items() if key in used}
    pins.update(own)
    return pins


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def recipe_entries(image, root=ROOT):
    base = root / image
    if not (base / "Dockerfile").is_file():
        raise SystemExit(f"{image}/Dockerfile not found")
    entries = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.endswith(".pyc"):
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(f"L {relative} {os.readlink(path)}\n")
            else:
                mode = "x" if os.access(path, os.X_OK) else "-"
                entries.append(f"F {relative} {mode} {file_digest(path)}\n")
    return sorted(entries)


def image_hash(image, _seen=(), root=ROOT, scoped=True):
    if image not in IMAGES:
        raise SystemExit(f"unknown image {image!r}; known: {', '.join(IMAGES)}")
    if image in _seen:
        raise SystemExit("parent cycle")
    digest = hashlib.sha256(f"malrunar {SCHEMA} {image}\n".encode())
    for entry in recipe_entries(image, root):
        digest.update(entry.encode())
    for key, value in sorted(merged_pins(image, root, scoped).items()):
        digest.update(f"PIN {key}={value}\n".encode())
    parent = IMAGES[image]
    if parent:
        digest.update(f"PARENT {parent} {image_hash(parent, _seen + (image,), root, scoped)}\n".encode())
    return digest.hexdigest()[:16]


def version(image, root=ROOT):
    pins = merged_pins(image, root)
    if VERSION_PIN not in pins:
        raise SystemExit(f"{image}/pins.env must define {VERSION_PIN} (the human-readable version tag)")
    return pins[VERSION_PIN]


def tags(image):
    return [version(image), "h-" + image_hash(image)]


def main(argv):
    if len(argv) < 2 or argv[1] not in {"list", "hash", "legacy-hash", "tags", "parent", "pins"}:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "list":
        print("\n".join(IMAGES))
        return 0
    all_pins = False
    if argv[1] == "pins" and len(argv) == 4 and argv[2] == "--all":
        all_pins = True
        argv = argv[:2] + argv[3:]
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    image = argv[2]
    if image not in IMAGES:
        raise SystemExit(f"unknown image {image!r}; known: {', '.join(IMAGES)}")
    if argv[1] == "hash":
        print(image_hash(image))
    elif argv[1] == "legacy-hash":
        print(image_hash(image, scoped=False))
    elif argv[1] == "tags":
        print("\n".join(tags(image)))
    elif argv[1] == "parent":
        print(IMAGES[image] or "")
    else:
        pins = merged_pins(image, scoped=not all_pins)
        print("\n".join(f"{k}={v}" for k, v in sorted(pins.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
