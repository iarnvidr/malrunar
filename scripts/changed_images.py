#!/usr/bin/env python3
"""Which images a change touches, so pull requests build and test only those (standard library only).

An image is changed when, between <base> (a git revision) and the working tree:
  * its content hash differs (recipe, the pins its Dockerfile uses, parent hash), or it is new, or
  * any file under <image>/ changed, including its test/ directory (not part of the hash), or
  * an image it is tested with changed (malrunar_image.TESTED_WITH: media-bridge and work-browser are
    smoke-tested inside a desktop-base image), or
  * scripts/build.sh, scripts/smoke.sh or the pull request workflow changed: then every image is
    rebuilt, because the way images are built or tested changed.
A pin that no Dockerfile of an image declares never changes that image.

Usage:
  changed_images.py <base-revision> [--plan]    changed images, one per line (all of them when <base> is "all")
  changed_images.py <base-revision> --republish  the images whose content hash differs: what publish.yml
                                                 would build and push after merging (no test-only reasons)
With --plan, prints GitHub Actions outputs (key=value): `images` (JSON list), `groups` (JSON list of
space-separated build chains for the `build` job), and one true/false per dedicated job.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import malrunar_image as mi  # noqa: E402

# Images built and smoke-tested one after another in one job: a child is built FROM its parent.
BUILD_GROUPS = (
    ("guacd",),
    ("tail-browser-mcp",),
    ("office-convert",),
    ("node",),
    ("desktop-base", "obs-runtime", "obs-control"),
)
# Images with a job of their own, and what runs them. Names are `plan` outputs.
DEDICATED = {
    "media-bridge": ("media-bridge",),
    "work-browser": ("work-browser",),
    "obs-control-e2e": ("obs-control", "media-bridge", "desktop-base", "obs-runtime"),
}
INFRASTRUCTURE_PATHS = ("scripts/build.sh", "scripts/smoke.sh", ".github/workflows/pr.yml")


def changed_images(base_root, head_root, changed_paths):
    """Images changed between two checkouts, given the repository paths that differ."""
    all_images = list(mi.IMAGES)
    if base_root is None or any(path in INFRASTRUCTURE_PATHS for path in changed_paths):
        return all_images
    changed = set()
    for image in all_images:
        if any(path.startswith(image + "/") for path in changed_paths):
            changed.add(image)
        elif not (base_root / image / "Dockerfile").is_file():
            changed.add(image)
        elif mi.image_hash(image, root=base_root) != mi.image_hash(image, root=head_root):
            changed.add(image)
    for image, partners in mi.TESTED_WITH.items():
        if changed & set(partners):
            changed.add(image)
    return [image for image in all_images if image in changed]


def republished_images(base_root, head_root):
    """Images publish.yml would build and push for head_root: new ones and those whose hash differs.
    Test-only reasons (a changed test/ directory, a changed desktop-base under a component) do not
    count: they do not change what is published."""
    out = []
    for image in mi.IMAGES:
        if not (base_root / image / "Dockerfile").is_file() \
                or mi.image_hash(image, root=base_root) != mi.image_hash(image, root=head_root):
            out.append(image)
    return out


def plan(images):
    chosen = set(images)
    groups = [" ".join(group) for group in BUILD_GROUPS if chosen & set(group)]
    out = {
        "images": json.dumps(sorted(chosen)),
        "groups": json.dumps(groups),
    }
    for job, triggers in DEDICATED.items():
        out[job.replace("-", "_")] = "true" if chosen & set(triggers) else "false"
    return out


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    base = args[0]
    head_root = mi.ROOT
    if base == "all":
        images = changed_images(None, head_root, [])
    else:
        paths = set(git("diff", "--name-only", base, cwd=head_root).decode().splitlines())
        paths |= set(git("ls-files", "--others", "--exclude-standard", cwd=head_root).decode().splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            archive = subprocess.run(["git", "archive", base], cwd=head_root, check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
            if "--republish" in argv:
                images = republished_images(Path(tmp), head_root)
            else:
                images = changed_images(Path(tmp), head_root, paths)
    if "--plan" in argv:
        for key, value in plan(images).items():
            print(f"{key}={value}")
    else:
        print("\n".join(images))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
