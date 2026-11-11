#!/usr/bin/env python3
"""Refresh the public Paseo and agent-dev image pins to upstream stable releases.

Only pinned inputs change. The PR workflow builds and smoke-tests the affected
images; the main publisher promotes those exact tested OCI artifacts by digest.
Neither this script nor the scheduled workflow deploys private consumers.

Usage:
  python3 -I scripts/bump_agent_runtimes.py [--dry-run]
  python3 -I scripts/bump_agent_runtimes.py --paseo-only [--dry-run]
  python3 -I scripts/bump_agent_runtimes.py --clis-only [--dry-run]
"""

import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "malrunar-agent-runtime-pin-updater"
PASEO_IMAGE = "ghcr.io/getpaseo/paseo"
ACCEPT_MANIFEST = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
STABLE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
PINNED_PASEO = re.compile(
    r"ghcr\.io/getpaseo/paseo:(\d+\.\d+\.\d+)@(sha256:[0-9a-f]{64})\Z"
)


def read_url(url, headers=None, method="GET"):
    request = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read(), response.headers


def json_url(url, headers=None):
    return json.loads(read_url(url, headers)[0])


def stable_version(version, source):
    if not isinstance(version, str) or not STABLE_VERSION.fullmatch(version):
        raise ValueError(f"{source}: expected a stable MAJOR.MINOR.PATCH, got {version!r}")
    return version


def version_key(version):
    return tuple(int(part) for part in stable_version(version, "version").split("."))


def paseo_latest():
    release = json_url(
        "https://api.github.com/repos/getpaseo/paseo/releases/latest",
        {"Accept": "application/vnd.github+json"},
    )
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("GitHub's latest Paseo release is not stable")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError(f"unexpected Paseo release tag {tag!r}")
    return stable_version(tag[1:], "Paseo GitHub release")


def ghcr_paseo_digest(version):
    """Resolve the tagged manifest/index digest, never an architecture-specific layer."""
    stable_version(version, "Paseo tag")
    token = json_url(
        "https://ghcr.io/token?service=ghcr.io&scope="
        + urllib.parse.quote("repository:getpaseo/paseo:pull")
    )["token"]
    _, headers = read_url(
        f"https://ghcr.io/v2/getpaseo/paseo/manifests/{version}",
        {"Authorization": f"Bearer {token}", "Accept": ACCEPT_MANIFEST},
        method="HEAD",
    )
    digest = headers.get("Docker-Content-Digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"invalid upstream Paseo manifest digest {digest!r}")
    return digest


def npm_latest(package):
    metadata = json_url(
        "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@") + "/latest"
    )
    if metadata.get("name") != package:
        raise ValueError(f"npm returned metadata for {metadata.get('name')!r}, not {package}")
    return stable_version(metadata.get("version"), f"npm {package}")


def pins(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line))


def set_pin(path, key, value, dry_run):
    original = path.read_text()
    current = pins(path).get(key)
    if current is None:
        raise ValueError(f"missing {key} in {path.relative_to(ROOT)}")
    if current == value:
        return
    changed, count = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", original, flags=re.M)
    if count != 1:
        raise ValueError(f"ambiguous {key} in {path.relative_to(ROOT)}")
    print(f"{path.relative_to(ROOT)}: {key}: {current} -> {value}")
    if not dry_run:
        path.write_text(changed)


def bump_paseo(root, dry_run):
    path = root / "paseo-runtime/pins.env"
    current = pins(path)
    version_match = re.fullmatch(r"(\d+\.\d+\.\d+)-malrunar([1-9]\d*)", current["VERSION"])
    image_match = PINNED_PASEO.fullmatch(current["PASEO_IMAGE"])
    if not version_match or not image_match or version_match.group(1) != image_match.group(1):
        raise ValueError("paseo-runtime version and digest-pinned official image disagree")

    latest = paseo_latest()
    previous = version_match.group(1)
    if version_key(latest) < version_key(previous):
        raise ValueError(f"refusing to downgrade Paseo from {previous} to {latest}")
    digest = ghcr_paseo_digest(latest)
    if latest == previous and digest != image_match.group(2):
        raise ValueError(f"upstream republished Paseo {latest} with a different digest; review manually")
    if latest == previous:
        return
    set_pin(path, "PASEO_IMAGE", f"{PASEO_IMAGE}:{latest}@{digest}", dry_run)
    set_pin(path, "VERSION", f"{latest}-malrunar1", dry_run)


def bump_clis(root, dry_run):
    updates = {
        "CODEX_VERSION": npm_latest("@openai/codex"),
        "CLAUDE_CODE_VERSION": npm_latest("@anthropic-ai/claude-code"),
    }
    for image in ("paseo-runtime", "agent-dev-base"):
        path = root / image / "pins.env"
        current = pins(path)
        for key, latest in updates.items():
            previous = stable_version(current[key], f"{image}/{key}")
            if version_key(latest) < version_key(previous):
                raise ValueError(f"refusing to downgrade {image}/{key}: {previous} -> {latest}")
            set_pin(path, key, latest, dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--paseo-only", action="store_true")
    selection.add_argument("--clis-only", action="store_true")
    args = parser.parse_args()
    if not args.clis_only:
        bump_paseo(ROOT, args.dry_run)
    if not args.paseo_only:
        bump_clis(ROOT, args.dry_run)


if __name__ == "__main__":
    main()
