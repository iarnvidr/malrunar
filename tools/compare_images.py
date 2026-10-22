#!/usr/bin/env python3
"""Prove two container images are functionally the same, or list exactly how they differ.

  compare_images.py manifest IMAGE OUT.json   describe an image: every path with
        type, mode, uid, gid, size, sha256 and link target (mtimes are ignored on
        purpose), the runtime configuration (env, entrypoint, healthcheck, user,
        workdir, ports, labels, volumes), the loader cache and the dpkg package list
  compare_images.py diff OLD.json NEW.json [--detail FILE] [--rename OLD=NEW ...]   report differences

`diff` attributes each differing path to the package that owns it when that package's version differs
between the images (a snapshot or archive date, not a layout change), applies --rules for the known
differences, and --noise (a second build of the new image) for build-time nondeterminism. It exits 1
for anything left unexplained or any configuration difference; --detail writes every path with its
explanation. `manifest` runs one container, bounded by `timeout`, to read the loader cache and the
package list (both empty for an image without bash, ldconfig or dpkg). Python standard library and the docker CLI only.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

# Created by `docker create`, not part of the image.
DOCKER_RUNTIME_PATHS = {".dockerenv", "etc/hostname", "etc/hosts", "etc/resolv.conf", "dev", "dev/console",
                        "dev/pts", "dev/shm", "proc", "sys", "etc/mtab"}
RUNTIME_PROBE_TIMEOUT = 300
CONFIG_KEYS = ["Env", "Entrypoint", "Cmd", "User", "WorkingDir", "ExposedPorts", "Healthcheck", "Labels",
               "Volumes", "Shell", "StopSignal", "OnBuild"]


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs).stdout


def manifest(image, out):
    inspected = json.loads(docker("image", "inspect", image))[0]
    config = {key: inspected["Config"].get(key) for key in CONFIG_KEYS}
    config["Architecture"], config["Os"] = inspected.get("Architecture"), inspected.get("Os")
    container = docker("create", image, "unused").strip()
    entries, dpkg_lists = {}, {}
    try:
        process = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                path = (member.name[2:] if member.name.startswith("./") else member.name).rstrip("/") or "."
                if path in DOCKER_RUNTIME_PATHS:
                    continue
                kind = ("file" if member.isfile() else "dir" if member.isdir() else "symlink" if member.issym()
                        else "hardlink" if member.islnk() else "other")
                record = {"type": kind, "mode": member.mode & 0o7777, "uid": member.uid, "gid": member.gid}
                if member.isfile():
                    digest = hashlib.sha256()
                    handle = archive.extractfile(member)
                    keep = path.startswith("var/lib/dpkg/info/") and path.endswith(".list")
                    kept = []
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(block)
                        if keep:
                            kept.append(block)
                    record.update(size=member.size, sha256=digest.hexdigest())
                    if keep:
                        # Which package owns which path: lets diff attribute changes to package updates.
                        dpkg_lists[path.rsplit("/", 1)[1][:-5]] = b"".join(kept).decode().split("\n")
                elif member.issym() or member.islnk():
                    record["link"] = member.linkname
                entries[path] = record
        if process.wait():
            raise SystemExit("docker export failed")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    # Bounded, and tolerant: an image without bash, ldconfig or dpkg (a FROM scratch component) has no
    # loader cache or package list to compare.
    probe = subprocess.run(
        ["timeout", "-k", "10", str(RUNTIME_PROBE_TIMEOUT), "docker", "run", "--rm", "--entrypoint", "bash", image, "-c",
         "ldconfig -p | sed 1d | sort; echo ----; dpkg-query -W -f='${binary:Package} ${Version}\\n' | sort"],
        capture_output=True, text=True)
    loader, _, packages = (probe.stdout if probe.returncode == 0 else "").partition("----\n")
    with open(out, "w") as handle:
        json.dump({"image": image, "id": inspected["Id"], "layers": len(inspected["RootFS"]["Layers"]),
                   "config": config, "entries": entries, "dpkg_lists": dpkg_lists, "ldconfig": loader.splitlines(),
                   "packages": packages.splitlines()}, handle)
    print(f"{image}: {len(entries)} paths, {len(inspected['RootFS']['Layers'])} layers -> {out}")


def area(path):
    parts = path.split("/")
    return "/".join(parts[:3]) if parts[0] in ("opt", "usr", "var") and len(parts) > 2 else "/".join(parts[:2])


def package_versions(manifest_data):
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in manifest_data["packages"] if " " in line}


def owners(manifest_data):
    """path -> owning package, from the image's own dpkg file lists."""
    result = {}
    for package, paths in manifest_data.get("dpkg_lists", {}).items():
        for path in paths:
            if path and path != "/.":
                result.setdefault(path.lstrip("/"), package)
    return result


def load_rules(path):
    """`regex<TAB>reason` per line; a path (or `config:<Key>`) matching a regex is an explained difference."""
    rules = []
    for line in (Path(path).read_text().splitlines() if path else []):
        if line.strip() and not line.startswith("#"):
            pattern, _, reason = line.partition("\t")
            rules.append((re.compile(pattern), reason.strip()))
    return rules


def differing_paths(a, b):
    """Paths whose record differs, or that exist in only one of two manifests."""
    result = set(a["entries"]) ^ set(b["entries"])
    result |= {p for p in set(a["entries"]) & set(b["entries"]) if a["entries"][p] != b["entries"][p]}
    return result


def apply_renames(data, renames):
    """View an OLD manifest as if its install prefixes had always been the new ones.

    `renames` is a list of (old, new) absolute paths. Entry paths, symlink targets, the runtime
    configuration (env, entrypoint, healthcheck, labels) and the loader cache are rewritten; file
    contents are not, so a file that embeds an old path still shows up as changed and needs a rule.
    """
    if not renames:
        return data
    patterns = [(re.compile(re.escape(old) + r"(?![\w.-])"), new) for old, new in renames]

    def text(value):
        for pattern, new in patterns:
            value = pattern.sub(new, value)
        return value

    def path(value):
        return text("/" + value)[1:]

    entries = {}
    for key, record in data["entries"].items():
        record = dict(record)
        if "link" in record:
            record["link"] = text(record["link"])
        entries[path(key)] = record
    config = json.loads(text(json.dumps(data["config"])))
    return {**data, "entries": entries, "config": config, "ldconfig": [text(line) for line in data["ldconfig"]]}


def diff(old_file, new_file, detail, rules_file=None, noise_file=None, renames=()):
    old, new = apply_renames(json.loads(Path(old_file).read_text()), renames), json.loads(Path(new_file).read_text())
    # A second build of the NEW image from the same source: whatever differs between the two is
    # build-time nondeterminism (random keys, caches, zip timestamps), not a property of the layout.
    noise = differing_paths(new, json.loads(Path(noise_file).read_text())) if noise_file else set()
    old_versions, new_versions = package_versions(old), package_versions(new)
    changed_packages = {p for p in set(old_versions) | set(new_versions) if old_versions.get(p) != new_versions.get(p)}
    old_owner, new_owner = owners(old), owners(new)
    rules = load_rules(rules_file)
    only_old = sorted(set(old["entries"]) - set(new["entries"]))
    only_new = sorted(set(new["entries"]) - set(old["entries"]))
    changed = []
    for path in sorted(set(old["entries"]) & set(new["entries"])):
        a, b = old["entries"][path], new["entries"][path]
        fields = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        if fields:
            changed.append((path, {k: (a.get(k), b.get(k)) for k in fields}))

    def explain(path):
        for package in (old_owner.get(path), new_owner.get(path)):
            if package in changed_packages:
                return f"package {package}: {old_versions.get(package, '-')} -> {new_versions.get(package, '-')}"
        for pattern, reason in rules:
            if pattern.search(path):
                return reason
        if path in noise:
            return "differs between two builds of the same source (build-time nondeterminism)"
        return None

    explained, unexplained = {}, []
    for kind, items in (("only-old", only_old), ("only-new", only_new), ("changed", [p for p, _ in changed])):
        for path in items:
            reason = explain(path)
            if reason:
                explained.setdefault(reason, []).append((kind, path))
            else:
                unexplained.append((kind, path))
    lines = []
    for title, items in (("only in OLD", only_old), ("only in NEW", only_new)):
        lines.append(f"== {title}: {len(items)}")
        lines += [f"  {p}  {(old if title.endswith('OLD') else new)['entries'][p]}  [{explain(p) or 'UNEXPLAINED'}]" for p in items]
    lines.append(f"== changed: {len(changed)}")
    lines += [f"  {p}  {fields}  [{explain(p) or 'UNEXPLAINED'}]" for p, fields in changed]
    config_diffs = [(k, old["config"].get(k), new["config"].get(k)) for k in sorted(set(old["config"]) | set(new["config"]))
                    if old["config"].get(k) != new["config"].get(k)]
    lines.append(f"== config differences: {len(config_diffs)}")
    lines += [f"  {k}: {a!r} -> {b!r}" for k, a, b in config_diffs]
    loader = sorted(set(old["ldconfig"]) ^ set(new["ldconfig"]))
    lines.append(f"== loader cache differences: {len(loader)}")
    lines += [f"  {line}" for line in loader]
    lines.append(f"== package differences: {len(changed_packages)}")
    lines += [f"  {p}: {old_versions.get(p, '-')} -> {new_versions.get(p, '-')}" for p in sorted(changed_packages)]
    print(f"old {old['image']} ({len(old['entries'])} paths, {old['layers']} layers)")
    print(f"new {new['image']} ({len(new['entries'])} paths, {new['layers']} layers)")
    identical = len(set(old["entries"]) & set(new["entries"])) - len(changed)
    print(f"identical paths: {identical}; only old {len(only_old)}; only new {len(only_new)}; changed {len(changed)}; "
          f"config {len(config_diffs)}; loader cache lines {len(loader)}; differing packages {len(changed_packages)}")
    print(f"explained path differences: {sum(len(v) for v in explained.values())}; unexplained: {len(unexplained)}")
    for reason, items in sorted(explained.items(), key=lambda kv: -len(kv[1])):
        if reason.startswith("package "):
            continue
        print(f"  explained [{reason}]: {len(items)}  e.g. {items[0][1]}")
    by_package = sum(len(v) for r, v in explained.items() if r.startswith("package "))
    print(f"  explained by {len([r for r in explained if r.startswith('package ')])} package version differences: {by_package} paths")
    groups = {}
    for kind, path in unexplained:
        groups.setdefault((kind, area(path)), []).append(path)
    for (kind, where), items in sorted(groups.items()):
        print(f"  UNEXPLAINED {kind:9} {where}: {len(items)}  e.g. {items[0]}")
    for k, a, b in config_diffs:
        reason = next((reason for pattern, reason in rules if pattern.search("config:" + k)), None)
        print(f"  config {k}: {a!r} -> {b!r}  [{reason or 'UNEXPLAINED'}]")
    if detail:
        Path(detail).write_text("\n".join(lines) + "\n")
    unexplained_config = [d for d in config_diffs if not any(pattern.search("config:" + d[0]) for pattern, _ in rules)]
    return 1 if unexplained or unexplained_config else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("manifest")
    a.add_argument("image")
    a.add_argument("out")
    b = sub.add_parser("diff")
    b.add_argument("old")
    b.add_argument("new")
    b.add_argument("--detail")
    b.add_argument("--rules", help="file of `regex<TAB>reason` lines for known, explained differences")
    b.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                   help="an install prefix that moved (repeatable), e.g. /opt/old=/opt/new; applied to OLD's paths, link targets and configuration")
    b.add_argument("--noise", help="manifest of a second build of the NEW image; paths that differ between the two builds are nondeterminism")
    args = parser.parse_args()
    if args.command == "manifest":
        manifest(args.image, args.out)
        return 0
    renames = [tuple(item.split("=", 1)) for item in args.rename]
    return diff(args.old, args.new, args.detail, args.rules, args.noise, renames)


if __name__ == "__main__":
    sys.exit(main())
