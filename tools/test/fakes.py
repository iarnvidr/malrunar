"""Fake docker and ssh executables for tests that must not need a Docker daemon (standard library only).

The fake docker keeps its image store in the file named by FAKE_STORE (one reference per line) and
logs every call to FAKE_LOG. The fake ssh runs the remote command locally with FAKE_STORE switched to
FAKE_REMOTE_STORE, so a test can hold a "local" and a "remote" Docker store in two files.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
store=$FAKE_STORE
has() { grep -qxF -- "$1" "$store" 2>/dev/null; }
case "$1" in
  image)
    case "$2" in
      inspect)
        ref=${@: -1}
        has "$ref" || exit 1
        case "$*" in
          *"{{.Size}}"*) echo 1048576 ;;
          *"{{.Id}}"*) echo "sha256:$(printf %s "$ref" | sha256sum | cut -d' ' -f1)" ;;
          *"RootFS.Layers"*) echo "sha256:x 3 layers" ;;
        esac ;;
    esac ;;
  info) echo "[[driver-type io.containerd.snapshotter.v1]]" ;;
  build)
    ref=; while [ $# -gt 0 ]; do [ "$1" = -t ] && ref=$2; shift; done
    [ -z "${FAKE_BUILD_FAILS:-}" ] || { echo "build failed on purpose"; exit 1; }
    echo "$ref" >> "$store" ;;
  tag) has "$2" || exit 1; echo "$3" >> "$store" ;;
  rmi) grep -vxF -- "$2" "$store" > "$store.new" || true; mv "$store.new" "$store" ;;
  save) echo "$2" ;;
  load) IFS= read -r ref; echo "$ref" >> "$store"; cat >/dev/null ;;
  run) cat > "${FAKE_RUN_STDIN:-/dev/null}"; exit "${FAKE_RUN_STATUS:-0}" ;;
esac
exit 0
'''
FAKE_SSH = r'''#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
cmd=${@: -1}
if [ -n "${FAKE_REMOTE_STORE:-}" ]; then export FAKE_STORE=$FAKE_REMOTE_STORE; fi
exec bash -c "$cmd"
'''
FAKE_TIMEOUT = r'''#!/usr/bin/env bash
echo "timeout $*" >> "$FAKE_LOG"
while [[ $1 == -* ]]; do case $1 in -k) shift 2 ;; *) shift ;; esac; done
shift
exec "$@"
'''


class FakeDockerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        self.log, self.store, self.remote_store = base / "log", base / "store", base / "remote-store"
        for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH), ("timeout", FAKE_TIMEOUT)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        for path in (self.log, self.store, self.remote_store):
            path.write_text("")

    def env(self, **extra):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "FAKE_STORE": str(self.store), "FAKE_REMOTE_STORE": str(self.remote_store), "HOME": self.tmp.name}
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.update(extra)
        return env

    def run_cmd(self, command, **extra):
        return subprocess.run(command, capture_output=True, text=True, env=self.env(**extra))

    def logged(self):
        return self.log.read_text()

    def lines(self, path):
        return [line for line in path.read_text().splitlines() if line]
