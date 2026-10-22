# tools

Build and deploy tooling for composable images: content-addressed hashing of a build graph, delivery
of an image to a Docker host that already holds most of its layers, assembly of a final image from a
base plus an explicit list of components, and a comparison that tells whether two images are the same.
Nothing here names a registry, host, product or path: they come from arguments, a manifest or the
environment. Bash, curl and Python 3.11+ (standard library only), plus the `docker` and `ssh` CLIs.

| Tool | What it does |
|---|---|
| [`scripts/malrunar_image.py`](../scripts/malrunar_image.py) | Content hashes and tags. Directory mode (one directory per image, used by this repository's workflows) and `graph` mode (a manifest of Dockerfile targets, parent-chained hashing) |
| [`malrunar_graph.py`](malrunar_graph.py) | Builds the nodes of a graph that a host lacks, sends them, assembles and checks the final image |
| [`deliver_image.sh`](deliver_image.sh) | Sends one image to a remote Docker daemon with a pinned `docker pussh`, or `docker save`/`load` |
| [`assemble-image.sh`](assemble-image.sh) | Base plus `COPY --link --from` components into a final image, checked before it is tagged |
| [`compare_images.py`](compare_images.py) | Describes an image (every path, configuration, packages) and diffs two of them |

There is no tool image: every tool is a script that runs where the Docker CLI and ssh are already
installed (a CI runner, an operator's machine), so an image would only add a second thing to keep
current. Tests: `python3 -m unittest discover -s tools/test` (also run by the `scripts` job of
`pr.yml` through `scripts/test/test_tools.py`). The test against a real Docker daemon
(`tools/test/test_integration.py`) builds [`example/`](example/) and is skipped when there is no daemon.

## The graph manifest

A graph is a set of Dockerfile targets. A typical one is a base image plus `FROM scratch` components
(each holds only its own prefix), assembled by `COPY --link --from=<component> / /`. The manifest is
TOML, [`example/graph.toml`](example/graph.toml) is a complete one:

```toml
schema = 1                 # required
epoch = "1"                # bump to rebuild every node (default "1")
namespace = "example"      # nodes are tagged <namespace>/<node>:<hash>
dockerfile = "Dockerfile"  # default Dockerfile, relative to the repository root
context = "."              # default build context, relative to the repository root

[build_args]               # optional: overrides the default of a global ARG; hashed, passed to the build
EXAMPLE_UBUNTU = "ubuntu:26.04@sha256:..."

[node.base]
target = "base"            # the Dockerfile target; without it the whole Dockerfile is the node
arg = "RUNTIME_BASE_IMAGE" # the ARG through which other stages receive this node's reference
# dockerfile, context  per node, when it has its own recipe
# extra_paths = [...]      repository paths hashed in addition to what the stages COPY
# ship = false             a local build input that is never sent or assembled (default true)

[node.alpha]
target = "c-alpha"
arg = "C_ALPHA_IMAGE"

[assemble]                 # optional
base = "base"              # the final image is FROM this node
components = ["alpha"]     # COPY --link --from each, in this order
final = "final.Dockerfile" # optional: lines appended to the final stage (ENV, WORKDIR, ENTRYPOINT)
checks = "checks.sh"       # optional: bash script run as root inside the candidate image
label = "example.components"  # label that records the exact component references (default malrunar.components)
tag = "example/assembled:latest"  # default final tag (--tag overrides)
```

Paths are relative to the repository root, which is the manifest's directory unless `--root` is given.
A node's stages receive its parents as build args: `FROM ${RUNTIME_BASE_IMAGE} AS build` or
`COPY --from=${OTHER_IMAGE} ...` in a stage the target needs makes that node a parent, the Dockerfile
declares `ARG RUNTIME_BASE_IMAGE` before its first `FROM`, and the tools pass the parent's
`<namespace>/<node>:<hash>` reference. Parents are built (or taken) first.

### What a node's hash covers

`h = sha256(schema, epoch, node name, the instructions of the stages the target needs (comments and
unrelated stages do not count), path + exec bit + content of every file those stages COPY, the
manifest's extra paths, every ARG those stages reference with its resolved value, the hash of every
parent node)`, shortened to 16 hex digits. It is computed offline and does not depend on where the
repository is checked out. A change below a node changes the node and everything built on it, and
nothing else. This is the same chain rule directory mode applies to `obs-runtime` on `desktop-base`.

```sh
python3 scripts/malrunar_image.py graph tools/example/graph.toml plan      # JSON: hash, ref, parents per node
python3 scripts/malrunar_image.py graph tools/example/graph.toml hash gamma
python3 scripts/malrunar_image.py graph tools/example/graph.toml assemble-dockerfile
python3 scripts/malrunar_image.py graph tools/example/graph.toml assemble-args
```

The directory mode of the same script (`list`, `hash`, `tags`, `parent`, `pins`) is unchanged.

## malrunar_graph.py: build, send, assemble

```sh
# Local: build every node, assemble, check. Nothing leaves the machine.
tools/malrunar_graph.py tools/example/graph.toml

# Deploy: ask the host what it holds, build and send only what is missing, assemble and check on the host.
tools/malrunar_graph.py tools/example/graph.toml --remote deploy@host.example \
    --known-hosts known_hosts --summary report.json
```

Options: `--remote USER@HOST` (or `MALRUNAR_GRAPH_REMOTE`), `--ssh-config`, `--ssh-key`,
`--known-hosts FILE` (trust only these host keys), `--method pussh|save-load|auto`
(`DELIVER_IMAGE_METHOD`), `--tag` (repeatable; replaces the manifest tag), `--components-only`,
`--jobs N` (`MALRUNAR_GRAPH_JOBS`, default 3), `--summary FILE` (JSON report), `--print-plan`
(hashes only, no Docker). With `GITHUB_STEP_SUMMARY` set it adds a table of what was built, sent
and how much travelled. A node the host lacks whose parent the host holds gets that parent copied
from the host; nodes are built in parent-first order, up to `--jobs` at a time. On the host the
assembly runs from files the tool streams over ssh, so nothing of this repository has to be installed
there except Docker.

## deliver_image.sh

```sh
tools/deliver_image.sh [--method pussh|save-load|auto] [--ssh-config F] [--known-hosts F] [--ssh-key F] IMAGE[:TAG] [USER@]HOST[:PORT]
```

`pussh` uses [unregistry](https://github.com/psviderski/unregistry)'s `docker pussh`: layers the
target already has are skipped. It needs the containerd image store on the target, which the script
checks first (`docker info` must report `io.containerd.snapshotter.v1`) and otherwise fails with a
message that says so; `auto` falls back to `save-load` (`docker save | gzip | ssh docker load`, the
whole image every time). The `docker-pussh` script is downloaded from a pinned commit and verified
against a pinned sha256 before it runs, and the unregistry image it starts on the target is pinned by
digest. The four `PINNED_*` values at the top of the script (version, commit, sha256, image digest)
are bumped together, by hand. After delivery the image ID on the target must equal the local one.
The script is licensed with this repository; `docker-pussh` and unregistry (Apache-2.0) are
downloaded at run time and not included.

## assemble-image.sh

```sh
tools/assemble-image.sh --dockerfile FILE --tag IMAGE[:TAG]... [--checks SCRIPT] ARG=image-ref...
```

Builds the generated assembly Dockerfile against images already in the local Docker store, runs the
checks script as root inside the candidate (`docker run --entrypoint bash ... -s`, bounded by
`timeout`, `ASSEMBLE_CHECK_TIMEOUT` seconds, default 900), and tags the result only if they pass, so
a failed assembly leaves the previous tag where it was. The checks run every time, since coupling
between the base and a component at run time does not show in any hash. `COPY --link` makes an
unchanged component a byte-identical layer.

## compare_images.py

```sh
tools/compare_images.py manifest IMAGE old.json
tools/compare_images.py manifest IMAGE2 new.json
tools/compare_images.py diff old.json new.json [--rules rules.tsv] [--rename /old/prefix=/new/prefix] [--noise second-build.json] [--detail all.txt]
```

A manifest lists every path with type, mode, uid, gid, size, sha256 and link target (mtimes are
ignored), the runtime configuration (env, entrypoint, healthcheck, user, workdir, ports, labels,
volumes), the loader cache and the package list. `diff` exits 1 for any path or configuration
difference it cannot explain. Explanations: a package whose version differs owns the path;
`--rules` (`regex<TAB>reason` per line, `config:<Key>` for configuration) for known differences;
`--rename` when an install prefix moved; `--noise`, a manifest of a second build of the new image,
for build-time nondeterminism. `manifest` runs one container (bounded) to read the loader cache and
package list; both are empty for an image without bash, ldconfig or dpkg.

## Composing with these tools

They are all opt-in and independent: use `scripts/malrunar_image.py graph` for the hashes only,
`deliver_image.sh` to send any local image, `assemble-image.sh` with a Dockerfile you wrote,
`compare_images.py` on any two images. `malrunar_graph.py` ties the first three together for a
manifest. Components of this repository (`node`, `work-browser`, ...) can be graph nodes
(`FROM ghcr.io/iarnvidr/node@sha256:...` as a stage, or a `COPY --from` of their published image) and
assembled the same way as the example. Nothing in a component depends on these tools.
