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

Graph mode (any repository layout, described by a manifest; see tools/README.md): the same
parent-chained content hashing for a build graph of Dockerfile stages that is not one directory per
image, for example a base image plus `FROM scratch` component targets of one Dockerfile.
  malrunar_image.py graph <manifest.toml> plan                 # JSON: every node's hash, ref, parents
  malrunar_image.py graph <manifest.toml> hash|ref <node>
  malrunar_image.py graph <manifest.toml> order                # nodes, parents first
  malrunar_image.py graph <manifest.toml> assemble-args        # ARG=ref lines for the assembly Dockerfile
  malrunar_image.py graph <manifest.toml> assemble-dockerfile  # the assembly Dockerfile (base + COPY --link --from list)
"""
import glob
import hashlib
import json
import os
import re
import shlex
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
    "thunderbird": None,
    "thunderbird-mcp": None,
}
# Images whose smoke test runs them inside or next to other images. This is test-time only (it is
# not part of the hash and not a build parent); changed_images.py re-tests them when those change.
TESTED_WITH = {
    "media-bridge": ("desktop-base",),
    "work-browser": ("desktop-base",),
    "session-kit": ("desktop-base", "work-browser"),
    "thunderbird": ("desktop-base",),
    "thunderbird-mcp": ("desktop-base", "node", "thunderbird"),
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


# ---------------------------------------------------------------------------------------------
# Graph mode: parent-chained hashing of Dockerfile build targets, described by a TOML manifest.
#
# A node is a Dockerfile (or one target of it). Its hash covers the instructions of the stages the
# target needs (the stages it builds FROM or COPY --from, with the ARGs they reference resolved,
# so base-image digests and snapshot dates are inputs), the content and exec bit of every
# repository file those stages COPY, the extra paths the manifest lists, the manifest epoch, and the
# hash of every other node the stages receive as a build arg. Nothing needs Docker.
# ---------------------------------------------------------------------------------------------
GRAPH_SCHEMA = 1
GRAPH_JUNK = {"__pycache__", "node_modules", ".git"}


class Stage:
    def __init__(self, name, base):
        self.name, self.base, self.lines, self.sources, self.from_stages = name, base, [], [], set()


def logical_lines(text):
    """Dockerfile instructions with continuations joined and comments removed."""
    out, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            current += raw.rstrip()[:-1] + " "
            continue
        out.append((current + raw).strip())
        current = ""
    if current:
        out.append(current.strip())
    return out


def parse_dockerfile(text):
    """(global ARG defaults, {stage name: Stage}). Sources are COPY/ADD paths from the build context."""
    global_args, stages, current = {}, {}, None
    for line in logical_lines(text):
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            words = [w for w in rest.split() if not w.startswith("--")]
            name = words[2] if len(words) >= 3 and words[1].lower() == "as" else f"#{len(stages)}"
            current = Stage(name, words[0])
            current.lines.append(line)
            stages[name] = current
            continue
        if current is None:
            if keyword == "ARG":
                key, _, value = rest.partition("=")
                global_args[key.strip()] = value.strip()
            continue
        current.lines.append(line)
        if keyword in ("COPY", "ADD"):
            words = shlex.split(rest)
            flags = [w for w in words if w.startswith("--")]
            args = [w for w in words if not w.startswith("--")]
            origin = next((f.split("=", 1)[1] for f in flags if f.startswith("--from=")), None)
            if origin:
                current.from_stages.add(origin)
            else:
                current.sources += [s for s in args[:-1] if not re.match(r"^https?://", s)]
    return global_args, stages


def stage_closure(stages, target):
    """Stages building `target` needs (all stages when target is None), and the ARG names their
    FROM lines and COPY --from flags reference."""
    if target is None:
        order = list(stages.values())
    else:
        if target not in stages:
            raise SystemExit(f"target {target!r} not found in the Dockerfile; stages: {', '.join(stages)}")
        seen, order, pending = set(), [], [target]
        while pending:
            name = pending.pop()
            if name in seen or name not in stages:
                continue
            seen.add(name)
            order.append(stages[name])
            pending.append(stages[name].base)
            pending.extend(stages[name].from_stages)
    arg_names = set()
    for stage in order:
        for value in (stage.base, *stage.from_stages):
            arg_names.update(re.findall(r"\$\{?(\w+)\}?", value))
    return sorted(order, key=lambda s: s.name), sorted(arg_names)


def hash_sources(root, sources, into):
    """Mix repository files (path, exec bit, content, symlink target) into a hash."""
    for source in sorted(set(sources)):
        matches = sorted(glob.glob(str(root / source.rstrip("/")), recursive=True))
        if not matches:
            raise SystemExit(f"COPY source not found: {source}")
        for match in matches:
            path = Path(match)
            entries = [path] if path.is_file() or path.is_symlink() else sorted(
                p for p in path.rglob("*")
                if not (GRAPH_JUNK & set(p.relative_to(path).parts)) and not p.name.endswith(".pyc"))
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    continue
                relative = entry.relative_to(root).as_posix()
                if entry.is_symlink():
                    into.update(f"L {relative} {os.readlink(entry)}\n".encode())
                else:
                    mode = "x" if os.access(entry, os.X_OK) else "-"
                    into.update(f"F {relative} {mode} {file_digest(entry)}\n".encode())


class Graph:
    """A build graph read from a manifest (format: tools/README.md)."""

    def __init__(self, manifest, root=None):
        import tomllib
        self.manifest = Path(manifest).resolve()
        self.root = Path(root).resolve() if root else self.manifest.parent
        data = tomllib.loads(self.manifest.read_text())
        if data.get("schema") != 1:
            raise SystemExit(f"{manifest}: schema must be 1")
        self.epoch = str(data.get("epoch", "1"))
        self.namespace = data.get("namespace", "")
        if not re.fullmatch(r"[a-z0-9]+([._/-][a-z0-9]+)*", self.namespace):
            raise SystemExit(f"{manifest}: namespace must be a lower-case repository prefix, got {self.namespace!r}")
        self.default_dockerfile = data.get("dockerfile", "Dockerfile")
        self.default_context = data.get("context", ".")
        self.build_args = {k: str(v) for k, v in data.get("build_args", {}).items()}
        self.nodes = data.get("node", {})
        if not self.nodes:
            raise SystemExit(f"{manifest}: no [node.<name>] tables")
        self.assemble = data.get("assemble", {})
        self.arg_node = {}
        self._parsed, self._hashes = {}, {}
        for name, node in self.nodes.items():
            if not re.fullmatch(r"[a-z0-9]+([._-][a-z0-9]+)*", name):
                raise SystemExit(f"{manifest}: bad node name {name!r}")
            arg = node.get("arg")
            if arg:
                if arg in self.arg_node:
                    raise SystemExit(f"{manifest}: build arg {arg} is used by {self.arg_node[arg]} and {name}")
                self.arg_node[arg] = name
        for member in self.assembly_members():
            if member not in self.nodes:
                raise SystemExit(f"{manifest}: [assemble] names unknown node {member!r}")
            if not self.nodes[member].get("arg"):
                raise SystemExit(f"{manifest}: node {member} is assembled, so it needs an `arg`")
            if not self.nodes[member].get("ship", True):
                raise SystemExit(f"{manifest}: node {member} has ship = false and cannot be assembled")
        self.order()  # reject cycles and unknown targets now

    def assembly_members(self):
        if not self.assemble:
            return []
        return [self.assemble.get("base", "")] + list(self.assemble.get("components", []))

    def shipped(self):
        return [n for n, v in self.nodes.items() if v.get("ship", True)]

    def dockerfile(self, name):
        return self.nodes[name].get("dockerfile", self.default_dockerfile)

    def context(self, name):
        return self.nodes[name].get("context", self.default_context)

    def parsed(self, name):
        path = self.dockerfile(name)
        if path not in self._parsed:
            file = self.root / path
            if not file.is_file():
                raise SystemExit(f"{self.manifest}: Dockerfile {path} (node {name}) not found")
            self._parsed[path] = parse_dockerfile(file.read_text())
        return self._parsed[path]

    def closure(self, name):
        _, stages = self.parsed(name)
        return stage_closure(stages, self.nodes[name].get("target"))

    def parents(self, name):
        """Nodes this node receives as build args (in its stages' FROM lines and COPY --from flags)."""
        _, arg_names = self.closure(name)
        return [self.arg_node[a] for a in arg_names if a in self.arg_node and self.arg_node[a] != name]

    def hash(self, name, _seen=()):
        if name in self._hashes:
            return self._hashes[name]
        if name not in self.nodes:
            raise SystemExit(f"unknown node {name!r}; known: {', '.join(self.nodes)}")
        if name in _seen:
            raise SystemExit("parent cycle: " + " -> ".join((*_seen, name)))
        global_args, _ = self.parsed(name)
        stages, arg_names = self.closure(name)
        digest = hashlib.sha256(f"graph {GRAPH_SCHEMA} {self.epoch} {name}\n".encode())
        for stage in stages:
            digest.update(("\n".join(stage.lines) + "\n").encode())
        context = self.context(name)
        hash_sources(self.root, [(Path(context) / s).as_posix() for st in stages for s in st.sources], digest)
        hash_sources(self.root, self.nodes[name].get("extra_paths", []), digest)
        text = "\n".join("\n".join(stage.lines) for stage in stages)
        resolved = {**global_args, **self.build_args}
        for arg in sorted(set(arg_names) | {a for a in resolved if re.search(rf"\$\{{?{a}\b", text)}):
            if arg in self.arg_node and self.arg_node[arg] != name:
                parent = self.arg_node[arg]
                digest.update(f"{arg}=node:{parent}:{self.hash(parent, (*_seen, name))}\n".encode())
            else:
                digest.update(f"{arg}={resolved.get(arg, '')}\n".encode())
        self._hashes[name] = digest.hexdigest()[:16]
        return self._hashes[name]

    def ref(self, name):
        return f"{self.namespace}/{name}:{self.hash(name)}"

    def order(self):
        """Nodes, parents first (manifest order otherwise)."""
        done, out = set(), []

        def visit(name, stack):
            if name in done:
                return
            if name in stack:
                raise SystemExit("parent cycle: " + " -> ".join((*stack, name)))
            for parent in self.parents(name):
                visit(parent, (*stack, name))
            done.add(name)
            out.append(name)
        for name in self.nodes:
            visit(name, ())
        return out

    def node_build_args(self, name):
        """`--build-arg` values for building a node: the refs of its parents and the manifest's build_args."""
        _, arg_names = self.closure(name)
        values = {a: self.ref(self.arg_node[a]) for a in arg_names if a in self.arg_node and self.arg_node[a] != name}
        global_args, _ = self.parsed(name)
        values.update({k: v for k, v in self.build_args.items() if k in global_args})
        return values

    def assemble_args(self):
        return [f"{self.nodes[n]['arg']}={self.ref(n)}" for n in self.assembly_members()]

    def assemble_dockerfile(self):
        if not self.assemble:
            raise SystemExit(f"{self.manifest}: no [assemble] table")
        base, components = self.assemble.get("base", ""), list(self.assemble.get("components", []))
        label = self.assemble.get("label", "malrunar.components")
        out = ["# Generated by scripts/malrunar_image.py graph assemble-dockerfile. Do not edit.",
               "# The base image plus one `COPY --link --from` per component, in manifest order.",
               f"ARG {self.nodes[base]['arg']}"]
        out += [f"ARG {self.nodes[c]['arg']}" for c in components]
        out += ["ARG ASSEMBLE_COMPONENTS=", ""]
        out += [f"FROM ${{{self.nodes[c]['arg']}}} AS use-{c}" for c in components]
        out += ["", f"FROM ${{{self.nodes[base]['arg']}}} AS final", "ARG ASSEMBLE_COMPONENTS",
                f'LABEL {label}="${{ASSEMBLE_COMPONENTS}}"']
        out += [f"COPY --link --from=use-{c} / /" for c in components]
        if self.assemble.get("final"):
            out += ["", (self.root / self.assemble["final"]).read_text().rstrip("\n")]
        return "\n".join(out) + "\n"

    def plan(self):
        return {n: {"hash": self.hash(n), "ref": self.ref(n), "parents": self.parents(n),
                    "ship": bool(self.nodes[n].get("ship", True)), "arg": self.nodes[n].get("arg", "")}
                for n in self.order()}


def graph_main(argv):
    if len(argv) < 2 or argv[1] not in {"plan", "hash", "ref", "order", "assemble-args", "assemble-dockerfile"}:
        print(__doc__, file=sys.stderr)
        return 2
    graph = Graph(argv[0])
    command = argv[1]
    if command == "plan":
        print(json.dumps(graph.plan(), indent=2))
    elif command in ("hash", "ref"):
        if len(argv) != 3:
            print(__doc__, file=sys.stderr)
            return 2
        print(graph.hash(argv[2]) if command == "hash" else graph.ref(argv[2]))
    elif command == "order":
        print("\n".join(graph.order()))
    elif command == "assemble-args":
        print("\n".join(graph.assemble_args()))
    else:
        sys.stdout.write(graph.assemble_dockerfile())
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "graph":
        return graph_main(argv[2:])
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
