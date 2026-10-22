#!/usr/bin/env python3
"""Content-addressed identity of the malrunar images (standard library only).

Every image has a hash that covers everything that can change it and can be
computed offline, without Docker:

  * the image's recipe: every file under <image>/ except test/ (Dockerfile,
    patches, scripts), each as path + exec bit + sha256;
  * the root pins.env and <image>/pins.env, parsed and sorted;
  * the hash of the parent image, if the image is built FROM another one of
    ours (obs-runtime is built FROM desktop-base), so a child is never paired
    with a different parent.

Tags published for an image: `<version>` (human, from <image>/VERSION or the
image's own VERSION pin) and `h-<hash>` (the content hash).

Usage:
  malrunar_image.py list
  malrunar_image.py hash <image>
  malrunar_image.py tags <image>          # one tag per line
  malrunar_image.py parent <image>        # parent image name or empty
  malrunar_image.py pins <image>          # merged root+image pins as KEY=VALUE
"""
import hashlib
import os
import sys
from pathlib import Path

SCHEMA = 1
ROOT = Path(__file__).resolve().parent.parent
# image -> parent image built FROM (None = built from the pinned Ubuntu base or scratch)
IMAGES = {"desktop-base": None, "obs-runtime": "desktop-base", "guacd": None, "tail-browser-mcp": None}
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


def merged_pins(image):
    pins = parse_pins(ROOT / "pins.env")
    own = parse_pins(ROOT / image / "pins.env")
    clash = set(pins) & set(own)
    if clash:
        raise SystemExit(f"{image}/pins.env redefines root pins: {sorted(clash)}")
    pins.update(own)
    return pins


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def recipe_entries(image):
    base = ROOT / image
    if not (base / "Dockerfile").is_file():
        raise SystemExit(f"{image}/Dockerfile not found")
    entries = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.endswith(".pyc"):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if path.is_symlink():
                entries.append(f"L {relative} {os.readlink(path)}\n")
            else:
                mode = "x" if os.access(path, os.X_OK) else "-"
                entries.append(f"F {relative} {mode} {file_digest(path)}\n")
    return sorted(entries)


def image_hash(image, _seen=()):
    if image not in IMAGES:
        raise SystemExit(f"unknown image {image!r}; known: {', '.join(IMAGES)}")
    if image in _seen:
        raise SystemExit("parent cycle")
    digest = hashlib.sha256(f"malrunar {SCHEMA} {image}\n".encode())
    for entry in recipe_entries(image):
        digest.update(entry.encode())
    for key, value in sorted(merged_pins(image).items()):
        digest.update(f"PIN {key}={value}\n".encode())
    parent = IMAGES[image]
    if parent:
        digest.update(f"PARENT {parent} {image_hash(parent, _seen + (image,))}\n".encode())
    return digest.hexdigest()[:16]


def version(image):
    pins = merged_pins(image)
    if VERSION_PIN not in pins:
        raise SystemExit(f"{image}/pins.env must define {VERSION_PIN} (the human-readable version tag)")
    return pins[VERSION_PIN]


def tags(image):
    return [version(image), "h-" + image_hash(image)]


def main(argv):
    if len(argv) < 2 or argv[1] not in {"list", "hash", "tags", "parent", "pins"}:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "list":
        print("\n".join(IMAGES))
        return 0
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    image = argv[2]
    if image not in IMAGES:
        raise SystemExit(f"unknown image {image!r}; known: {', '.join(IMAGES)}")
    if argv[1] == "hash":
        print(image_hash(image))
    elif argv[1] == "tags":
        print("\n".join(tags(image)))
    elif argv[1] == "parent":
        print(IMAGES[image] or "")
    else:
        print("\n".join(f"{k}={v}" for k, v in sorted(merged_pins(image).items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
