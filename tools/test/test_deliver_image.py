"""tools/deliver_image.sh with fake docker, ssh and curl (standard library only)."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deliver_image.sh"
IMAGE_ID = "sha256:" + "ab" * 32

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$1 $2" in
  "image inspect") [ "$3" = --format ] && echo "$FAKE_LOCAL_ID" || true ;;
  "save "*|"save"*) printf 'fake-image-bytes' ;;
esac
"""
FAKE_SSH = """#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  *"docker info"*) echo "$FAKE_REMOTE_STORE" ;;
  *"docker load"*) cat >/dev/null ;;
  *"image inspect"*) echo "${FAKE_REMOTE_ID:-$FAKE_LOCAL_ID}" ;;
esac
"""
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
out=; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
printf 'not the pinned script' > "$out"
"""


class DeliverImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "log"
        for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH), ("curl", FAKE_CURL)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_script(self, *args, store="[[driver-type io.containerd.snapshotter.v1]]", remote_id=None, env=None):
        environment = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                       "FAKE_LOCAL_ID": IMAGE_ID, "FAKE_REMOTE_STORE": store,
                       "DELIVER_IMAGE_TOOLS_DIR": str(Path(self.tmp.name) / "tools"), "HOME": self.tmp.name}
        environment.pop("DELIVER_IMAGE_METHOD", None)
        environment.pop("GITHUB_STEP_SUMMARY", None)
        if remote_id:
            environment["FAKE_REMOTE_ID"] = remote_id
        environment.update(env or {})
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=environment)
        return result, (self.log.read_text() if self.log.exists() else "")

    def test_requires_image_and_target(self):
        result, _ = self.run_script("only-one-argument")
        self.assertEqual(result.returncode, 2)
        self.assertIn("IMAGE[:TAG]", result.stderr + result.stdout)

    def test_unknown_method_is_rejected(self):
        result, _ = self.run_script("--method", "rsync", "img:1", "u@h")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown method", result.stderr)

    def test_save_load_streams_the_image_and_verifies_the_remote_id(self):
        result, log = self.run_script("--method", "save-load", "img:1", "u@h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker save img:1", log)
        self.assertIn("docker load", log)
        self.assertIn("by save-load", result.stdout)

    def test_pussh_refuses_a_non_containerd_target(self):
        result, log = self.run_script("--method", "pussh", "img:1", "u@h", store="[[Backing Filesystem extfs]]")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs the containerd image store", result.stderr)
        self.assertNotIn("curl", log)
        self.assertNotIn("docker save", log)

    def test_auto_falls_back_to_save_load_without_containerd(self):
        result, log = self.run_script("--method=auto", "img:1", "u@h", store="[[Backing Filesystem extfs]]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker save img:1", log)

    def test_method_can_come_from_the_environment(self):
        result, log = self.run_script("img:1", "u@h", env={"DELIVER_IMAGE_METHOD": "save-load"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker save", log)

    def test_pussh_script_must_match_its_pinned_checksum(self):
        result, log = self.run_script("--method", "pussh", "img:1", "u@h")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed its pinned checksum", result.stderr)
        self.assertIn("raw.githubusercontent.com/psviderski/unregistry/", log)
        self.assertFalse((Path(self.tmp.name) / "tools/docker-pussh-0.4.3").exists())

    def test_a_remote_image_id_mismatch_fails(self):
        result, _ = self.run_script("--method", "save-load", "img:1", "u@h", remote_id="sha256:" + "cd" * 32)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image ID mismatch", result.stderr)

    def test_known_hosts_are_pinned_through_a_generated_ssh_config(self):
        known_hosts = Path(self.tmp.name) / "known_hosts"
        known_hosts.write_text("h ssh-ed25519 AAAA\n")
        result, log = self.run_script("--method", "save-load", "--known-hosts", str(known_hosts), "img:1", "u@h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-F ", log)

    def test_pins_are_complete(self):
        text = SCRIPT.read_text()
        for pin in ("PINNED_PUSSH_VERSION=", "PINNED_PUSSH_COMMIT=", "PINNED_PUSSH_SHA256=", "PINNED_UNREGISTRY_IMAGE="):
            self.assertIn(pin, text)
        self.assertRegex(text, r"PINNED_PUSSH_SHA256=[0-9a-f]{64}\n")
        self.assertRegex(text, r"PINNED_UNREGISTRY_IMAGE=\S+@sha256:[0-9a-f]{64}\n")
        self.assertRegex(text, r"PINNED_PUSSH_COMMIT=[0-9a-f]{40}\n")
        self.assertRegex(text, r"PINNED_PUSSH_VERSION=\d+\.\d+\.\d+\n")


if __name__ == "__main__":
    unittest.main()
