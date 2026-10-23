#!/usr/bin/env python3
"""Build, deliver and assemble a graph of content-addressed images (standard library only).

  malrunar_graph.py MANIFEST [--remote USER@HOST] [options]

The manifest (tools/README.md) describes the nodes: Dockerfile targets such as a base image and
`FROM scratch` components. Every node is tagged `<namespace>/<node>:<hash>`; the hash comes from
`scripts/malrunar_image.py graph` and covers everything that can change the node, parents
included, so an unchanged node is never rebuilt or sent again.

Local mode (no --remote): build every node on this machine, then assemble the [assemble] image here
(what a pull request runs). Nothing is sent anywhere.

Remote mode (--remote): ask the remote Docker daemon which shipped nodes it holds, build only the
missing ones here (a missing node's parents are copied from the remote when it has them, rebuilt
otherwise), send each with tools/deliver_image.sh, then assemble and check the image on the remote
with tools/assemble-image.sh, which tags it only when the checks pass.
"""
import argparse
import concurrent.futures
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import malrunar_image as mi  # noqa: E402

DELIVER = HERE / "deliver_image.sh"
ASSEMBLE = HERE / "assemble-image.sh"


def log(message):
    print(message, flush=True)


class Runner:
    def __init__(self, graph, remote, ssh, jobs, tags, method, docker="docker"):
        self.graph, self.remote, self.ssh, self.jobs, self.tags, self.method = graph, remote, ssh, jobs, tags, method
        self.docker_cmd = docker
        self.report = {}

    # -- docker / ssh
    def docker(self, *args, **kwargs):
        return subprocess.run([self.docker_cmd, *args], **kwargs)

    def local_has(self, ref):
        return self.docker("image", "inspect", ref, capture_output=True).returncode == 0

    def ssh_base(self):
        command = ["ssh", "-o", "BatchMode=yes"]
        if self.ssh.get("ssh_config"):
            command += ["-F", self.ssh["ssh_config"]]
        if self.ssh.get("ssh_key"):
            command += ["-i", self.ssh["ssh_key"]]
        if self.ssh.get("known_hosts"):
            command += ["-o", f"UserKnownHostsFile={self.ssh['known_hosts']}", "-o", "StrictHostKeyChecking=yes"]
        return command

    def remote_holds(self, refs):
        script = ("for r in " + " ".join(shlex.quote(r) for r in refs)
                  + '; do docker image inspect "$r" >/dev/null 2>&1 && echo "$r"; done; true')
        result = subprocess.run([*self.ssh_base(), self.remote, script], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("cannot ask the remote Docker daemon which images it has: " + result.stderr.strip()[-300:])
        return set(result.stdout.split())

    def fetch_from_remote(self, ref):
        started = time.time()
        pipeline = (f"{' '.join(shlex.quote(a) for a in self.ssh_base())} {shlex.quote(self.remote)} "
                    f"{shlex.quote('docker save ' + ref + ' | gzip -1')} | gzip -d | {shlex.quote(self.docker_cmd)} load")
        result = subprocess.run(["bash", "-o", "pipefail", "-c", pipeline], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit(f"copying {ref} from {self.remote} failed: {result.stderr.strip()[-300:]}")
        log(f"copied {ref} from {self.remote} in {time.time() - started:.0f}s")

    def ship(self, ref):
        command = ["bash", str(DELIVER), "--method", self.method]
        for flag, key in (("--known-hosts", "known_hosts"), ("--ssh-config", "ssh_config"), ("--ssh-key", "ssh_key")):
            if self.ssh.get(key):
                command += [flag, self.ssh[key]]
        result = subprocess.run([*command, ref, self.remote], capture_output=True, text=True)
        if result.returncode:
            print(result.stdout[-3000:], result.stderr[-3000:])
            raise SystemExit(f"delivering {ref} to {self.remote} failed")
        return result.stdout.strip().splitlines()[-1]

    # -- building
    def build(self, name):
        graph = self.graph
        node = graph.nodes[name]
        command = ["build", "--progress=plain", "-t", graph.ref(name), "-f", graph.dockerfile(name)]
        if node.get("target"):
            command += ["--target", node["target"]]
        for key, value in graph.node_build_args(name).items():
            command += ["--build-arg", f"{key}={value}"]
        command.append(graph.context(name))
        with tempfile.NamedTemporaryFile("w+", prefix=f"build-{name}.", suffix=".log", delete=False) as logfile:
            result = self.docker(*command, cwd=graph.root, stdout=logfile, stderr=subprocess.STDOUT)
            if result.returncode:
                logfile.flush()
                print(Path(logfile.name).read_text()[-6000:])
                raise SystemExit(f"build failed: {name} (log {logfile.name})")
        os.unlink(logfile.name)

    def ensure(self, name, must_build, on_remote):
        """Make `name` available here; send it to the remote when the remote lacks it."""
        ref = self.graph.ref(name)
        entry = {"name": name, "hash": self.graph.hash(name), "ref": ref}
        started = time.time()
        if self.local_has(ref):
            entry["source"] = "local"
        elif name in on_remote and name not in must_build:
            self.fetch_from_remote(ref)
            entry["source"] = "copied from remote"
        else:
            self.build(name)
            entry["source"] = "built"
        entry["seconds"] = round(time.time() - started, 1)
        size = self.docker("image", "inspect", "--format", "{{.Size}}", ref, capture_output=True, text=True)
        entry["bytes"] = int(size.stdout.strip() or 0)
        if self.remote and name in must_build:
            shipped = time.time()
            entry["delivery"] = self.ship(ref)
            entry["shipped"] = True
            entry["ship_seconds"] = round(time.time() - shipped, 1)
        log(f"{name}: {entry['source']}{', shipped' if entry.get('shipped') else ''} {ref} ({entry['seconds']}s)")
        if entry.get("delivery"):
            log("  " + entry["delivery"])
        return entry

    def run(self, components_only=False):
        graph = self.graph
        order = graph.order()
        shipped = [n for n in order if graph.nodes[n].get("ship", True)]
        on_remote = set()
        if self.remote:
            held = self.remote_holds([graph.ref(n) for n in shipped])
            on_remote = {n for n in shipped if graph.ref(n) in held}
        must_build = {n for n in shipped if n not in on_remote} if self.remote else set(order)
        needed, pending = set(must_build), list(must_build)
        while pending:  # a node that must be built needs its parents here, whatever the remote holds
            for parent in graph.parents(pending.pop()):
                if parent not in needed:
                    needed.add(parent)
                    if parent not in on_remote:  # one the remote holds is copied from it: its own parents are not needed
                        pending.append(parent)
        if self.remote:
            log(f"{self.remote} holds: {sorted(on_remote) or 'nothing'}; to build and send: {sorted(must_build & set(shipped)) or 'nothing'}")
        else:
            log("local build of: " + ", ".join(order))
        needs = {n: set(graph.parents(n)) & needed for n in needed}
        done, running = {}, {}
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as pool:
            while len(done) < len(needed):
                for name in order:
                    if name in needed and name not in done and name not in running and needs[name] <= set(done):
                        running[name] = pool.submit(self.ensure, name, must_build, on_remote)
                finished, _ = concurrent.futures.wait(running.values(), return_when=concurrent.futures.FIRST_COMPLETED)
                for name in [n for n, f in running.items() if f in finished]:
                    done[name] = running.pop(name).result()
        self.report = {"remote_held": sorted(on_remote), "components": [done[n] for n in order if n in done],
                       "components_seconds": round(time.time() - started, 1),
                       "hashes": {n: graph.hash(n) for n in shipped}}
        if not components_only:
            if graph.assemble:
                self.assemble()
            else:
                log("the manifest has no [assemble] table: nothing to assemble")
        return self.report

    def assemble(self):
        graph = self.graph
        tags = self.tags or ([graph.assemble["tag"]] if graph.assemble.get("tag") else [])
        if not tags:
            raise SystemExit("no final image tag: pass --tag or set `tag` in [assemble]")
        files = {"assemble-image.sh": ASSEMBLE.read_bytes(),
                 "Dockerfile.assemble": graph.assemble_dockerfile().encode()}
        if graph.assemble.get("checks"):
            files["checks.sh"] = (graph.root / graph.assemble["checks"]).read_bytes()
        started = time.time()
        if self.remote:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o644
                    archive.addfile(info, io.BytesIO(data))
            script = (f'd=$(mktemp -d) && trap \'rm -rf "$d"\' EXIT && tar -xf - -C "$d" && bash "$d/assemble-image.sh" '
                      f'--dockerfile "$d/Dockerfile.assemble" ' + " ".join(f"--tag {shlex.quote(t)}" for t in tags)
                      + (' --checks "$d/checks.sh"' if "checks.sh" in files else "") + " "
                      + " ".join(shlex.quote(a) for a in graph.assemble_args()))
            result = subprocess.run([*self.ssh_base(), self.remote, script], input=buffer.getvalue())
        else:
            with tempfile.TemporaryDirectory(prefix="assemble.") as tmp:
                for name, data in files.items():
                    (Path(tmp) / name).write_bytes(data)
                command = ["bash", str(ASSEMBLE), "--dockerfile", str(Path(tmp) / "Dockerfile.assemble")]
                for tag in tags:
                    command += ["--tag", tag]
                if "checks.sh" in files:
                    command += ["--checks", str(Path(tmp) / "checks.sh")]
                result = subprocess.run([*command, *graph.assemble_args()], cwd=graph.root)
        if result.returncode:
            raise SystemExit("assembling or checking the image failed")
        self.report["assemble_seconds"] = round(time.time() - started, 1)
        self.report["final"] = {"tags": tags, "location": self.remote or "local"}


def write_summary(report, handle):
    handle.write("### Image graph\n\nRemote held: %s\n\n| node | hash | source | sent | build s | send s | image MiB | egress MiB |\n"
                 "|---|---|---|---|---|---|---|---|\n" % (", ".join(report["remote_held"]) or "nothing (or local build)"))
    for c in report["components"]:
        sent = re.search(r"egress ~(\d+) MiB", c.get("delivery") or "")
        handle.write(f"| {c['name']} | `{c['hash']}` | {c['source']} | {'yes' if c.get('shipped') else 'no'} | "
                     f"{c['seconds']} | {c.get('ship_seconds', '')} | {c['bytes'] // 1048576} | {sent[1] if sent else ''} |\n")
    handle.write(f"\nAssembled and checked in {report.get('assemble_seconds', '-')}s; total {report['seconds']}s.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("manifest")
    parser.add_argument("--root", help="repository root the manifest's paths are relative to (default: the manifest's directory)")
    parser.add_argument("--remote", default=os.environ.get("MALRUNAR_GRAPH_REMOTE", ""),
                        help="USER@HOST of the Docker host: ask it what it holds, send what it lacks, assemble there")
    parser.add_argument("--known-hosts", help="trust only these host keys for --remote")
    parser.add_argument("--ssh-config", help="ssh_config for --remote (default: the caller's)")
    parser.add_argument("--ssh-key", help="private key for --remote")
    parser.add_argument("--method", default=os.environ.get("DELIVER_IMAGE_METHOD", "pussh"),
                        choices=["pussh", "save-load", "auto"], help="delivery method (tools/deliver_image.sh)")
    parser.add_argument("--print-plan", action="store_true", help="print every node's hash and exit (no Docker needed)")
    parser.add_argument("--components-only", action="store_true", help="build (and send) nodes, do not assemble")
    parser.add_argument("--tag", action="append", default=[], help="final image tag (default: `tag` of [assemble])")
    parser.add_argument("--summary", help="write the JSON report here")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("MALRUNAR_GRAPH_JOBS", "3")))
    args = parser.parse_args(argv)
    graph = mi.Graph(args.manifest, args.root)
    if args.print_plan:
        print(json.dumps(graph.plan(), indent=2))
        return 0
    started = time.time()
    ssh = {"known_hosts": args.known_hosts, "ssh_config": args.ssh_config, "ssh_key": args.ssh_key}
    runner = Runner(graph, args.remote, ssh, args.jobs, args.tag, args.method)
    report = runner.run(components_only=args.components_only)
    report["seconds"] = round(time.time() - started, 1)
    if args.summary:
        Path(args.summary).write_text(json.dumps(report, indent=2) + "\n")
    log("finished in %ss; " % report["seconds"] + ", ".join(
        f"{c['name']}={c['source']}{'+sent' if c.get('shipped') else ''}" for c in report["components"]))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            write_summary(report, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
