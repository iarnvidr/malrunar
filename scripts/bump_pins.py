#!/usr/bin/env python3
"""Bump the moving pins of every image in place (standard library only, no credentials).

  * root pins.env: APT_SNAPSHOT (newest snapshot.ubuntu.com timestamp that serves the release) and
    UBUNTU_IMAGE (current digest of the pinned tag on Docker Hub);
  * tail-browser-mcp/pins.env: NODE_IMAGE digest and DEBIAN_SNAPSHOT;
  * desktop-base/pins.env: FIREFOX_ESR_VERSION and FIREFOX_ESR_SHA256 (current ESR from Mozilla's
    product-details, sha256 from the release's SHA256SUMS on archive.mozilla.org).

Prints one line per change and rewrites only the lines that changed. Review the resulting diff:
the content hashes (scripts/malrunar_image.py) change with every pin.
Usage: bump_pins.py [--dry-run]
"""
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UBUNTU_SERIES = "resolute"  # 26.04
DEBIAN_SERIES = "bookworm"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"])


def fetch(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), response.headers


def get_pins(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if re.match(r"^[A-Z0-9_]+=", line))


def set_pin(path, key, value, dry_run):
    text = path.read_text()
    old = get_pins(path).get(key)
    if old == value:
        return
    print(f"{path.relative_to(ROOT)}: {key}: {old} -> {value}")
    if not dry_run:
        path.write_text(re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M))


def snapshot_timestamp(base, series):
    """Newest hourly timestamp (three hours back, so the mirror has it) whose Release file exists."""
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    for _ in range(24):
        stamp = when.strftime("%Y%m%dT%H0000Z")
        try:
            fetch(f"{base}/{stamp}/dists/{series}/InRelease", method="HEAD")
            return stamp
        except urllib.error.URLError:
            when -= datetime.timedelta(hours=1)
    raise SystemExit(f"no snapshot found under {base}")


def docker_hub_digest(reference):
    """Current index digest of a Docker Hub `name:tag@sha256:old` reference (anonymous pull token)."""
    match = re.fullmatch(r"([a-z0-9._-]+):([A-Za-z0-9._-]+)@sha256:[0-9a-f]{64}", reference)
    if not match:
        raise SystemExit(f"unsupported image reference {reference!r}")
    name, tag = match.groups()
    token = json.loads(fetch(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/{name}:pull")[0])["token"]
    _, headers = fetch(f"https://registry-1.docker.io/v2/library/{name}/manifests/{tag}",
                       {"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES}, method="HEAD")
    digest = headers["Docker-Content-Digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"bad digest {digest!r} for {reference}")
    return f"{name}:{tag}@{digest}"


def firefox_esr():
    versions = json.loads(fetch("https://product-details.mozilla.org/1.0/firefox_versions.json")[0])
    version = versions["FIREFOX_ESR"]
    if not re.fullmatch(r"\d+(\.\d+)*esr", version):
        raise SystemExit(f"unexpected ESR version {version!r}")
    sums = fetch(f"https://archive.mozilla.org/pub/firefox/releases/{version}/SHA256SUMS")[0].decode()
    wanted = f"linux-x86_64/en-US/firefox-{version}.tar.xz"
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        if name == wanted:
            return version, digest
    raise SystemExit(f"{wanted} not in SHA256SUMS")


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    root_pins = ROOT / "pins.env"
    set_pin(root_pins, "APT_SNAPSHOT",
            snapshot_timestamp("https://snapshot.ubuntu.com/ubuntu", UBUNTU_SERIES), dry_run)
    set_pin(root_pins, "UBUNTU_IMAGE", docker_hub_digest(get_pins(root_pins)["UBUNTU_IMAGE"]), dry_run)
    tail = ROOT / "tail-browser-mcp/pins.env"
    set_pin(tail, "NODE_IMAGE", docker_hub_digest(get_pins(tail)["NODE_IMAGE"]), dry_run)
    set_pin(tail, "DEBIAN_SNAPSHOT",
            snapshot_timestamp("http://snapshot.debian.org/archive/debian", DEBIAN_SERIES), dry_run)
    desktop = ROOT / "desktop-base/pins.env"
    if desktop.exists():
        version, digest = firefox_esr()
        set_pin(desktop, "FIREFOX_ESR_VERSION", version, dry_run)
        set_pin(desktop, "FIREFOX_ESR_SHA256", digest, dry_run)
        if "VERSION" in get_pins(desktop) and get_pins(desktop)["VERSION"].endswith("esr"):
            set_pin(desktop, "VERSION", version, dry_run)


if __name__ == "__main__":
    main()
