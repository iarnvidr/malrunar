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
  changed_images.py --only <a,b,...> [--level L] --plan
                                                 exactly the named images (a manual dispatch of pr.yml, see
                                                 scripts/ci-try.sh): no TESTED_WITH expansion. The build chain
                                                 of a named image includes its parents (desktop-base,
                                                 obs-runtime, obs-control); the dedicated jobs pull their
                                                 parents from the registry by hash and build only what is missing.
                                                 --level unit|smoke|e2e|all keeps only the matching jobs.
With --plan, prints GitHub Actions outputs (key=value): `images` (JSON list), `groups` (JSON list of
space-separated build chains for the `build` job), `requested` (selected images, excluding unchanged
parents that can be pulled by content hash), and one true/false per dedicated job.
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
    ("guacamole-tunnel",),
    ("tail-browser-mcp",),
    ("office-convert",),
    ("node",),
    ("devops-service",),
    ("desktop-base", "obs-runtime", "obs-control"),
)
# Images with a job of their own, and what runs them. Names are `plan` outputs.
DEDICATED = {
    "media-bridge": ("media-bridge",),
    "work-browser": ("work-browser",),
    "session-kit": ("session-kit",),
    "thunderbird": ("thunderbird",),
    "thunderbird-mcp": ("thunderbird-mcp",),
    "msteams-mcp": ("msteams-mcp",),
    "teams-service": ("teams-service",),
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


LEVELS = ("unit", "smoke", "e2e", "all")


def plan(images, level="all"):
    """Job switches for `images`. level: unit = no image job (the `scripts` job only), smoke = every job
    but the end-to-end one, e2e = the end-to-end job only, all = everything."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    chosen = set(images)
    if level == "unit":
        chosen = set()
    groups = [" ".join(group) for group in BUILD_GROUPS if chosen & set(group)]
    if level == "e2e":
        groups = []
    out = {
        "images": json.dumps(sorted(chosen)),
        "groups": json.dumps(groups),
        "requested": " ".join(sorted(chosen)),
    }
    for job, triggers in DEDICATED.items():
        wanted = bool(chosen & set(triggers))
        if level == "smoke" and job == "obs-control-e2e":
            wanted = False
        if level == "e2e" and job != "obs-control-e2e":
            wanted = False
        out[job.replace("-", "_")] = "true" if wanted else "false"
    return out


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def option(argv, name):
    """The value after `name` in argv, or None."""
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv):
    only, level = option(argv, "--only"), option(argv, "--level") or "all"
    values = {only, level} - {None}
    args = [a for a in argv[1:] if not a.startswith("--") and a not in values]
    if level not in LEVELS or (only is not None and args) or (only is None and len(args) != 1):
        print(__doc__, file=sys.stderr)
        return 2
    head_root = mi.ROOT
    if only is not None:
        images = [name for name in only.replace(" ", ",").split(",") if name]
        unknown = [name for name in images if name not in mi.IMAGES]
        if unknown or not images:
            print(f"unknown component(s): {', '.join(unknown) or '(none given)'}; known: {', '.join(mi.IMAGES)}", file=sys.stderr)
            return 2
        images = [image for image in mi.IMAGES if image in images]
    elif args[0] == "all":
        images = changed_images(None, head_root, [])
    else:
        base = args[0]
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
        for key, value in plan(images, level).items():
            print(f"{key}={value}")
    else:
        print("\n".join(images))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
