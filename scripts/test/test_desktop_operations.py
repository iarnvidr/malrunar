"""The desktop-operations examples: the stale X lock cleaner and the xorgxrdp GPU Device section."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES = ROOT / "examples" / "desktop-operations"


class CleanStaleXLocks(unittest.TestCase):
    def run_script(self, directory):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MALRUNAR_X_TMP_DIR": str(directory)}
        subprocess.run(["/bin/sh", str(EXAMPLES / "clean-stale-x-locks.sh")], check=True, env=env)

    def test_removes_only_the_xrdp_range_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".X11-unix").mkdir()
            stale = [root / f".X{n}-lock" for n in (10, 13, 15, 19)] + [root / ".X11-unix" / f"X{n}" for n in (10, 13, 15, 19)]
            kept = [root / ".X9-lock", root / ".X20-lock", root / ".X99-lock", root / ".X11-unix" / "X9",
                    root / ".X11-unix" / "X99", root / "other-file"]
            for path in stale + kept:
                path.write_text("1")
            self.run_script(root)
            self.run_script(root)
            self.assertFalse(any(path.exists() for path in stale))
            self.assertTrue(all(path.exists() for path in kept))

    def test_missing_files_and_directories_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_script(Path(tmp))  # no .X11-unix directory at all

    def test_the_script_is_executable_posix_sh(self):
        script = EXAMPLES / "clean-stale-x-locks.sh"
        self.assertTrue(os.access(script, os.X_OK))
        self.assertEqual(script.read_text().splitlines()[0], "#!/bin/sh")


class GpuDeviceSection(unittest.TestCase):
    def test_virtio_gpu_is_allowed_next_to_the_stock_drivers(self):
        text = (EXAMPLES / "xorgxrdp-gpu.conf").read_text()
        options = dict(re.findall(r'Option "([^"]+)" "([^"]*)"', text))
        self.assertEqual(options["DRMDevice"], "/dev/dri/renderD128")  # a render node, never card0
        self.assertEqual(options["DRI3"], "1")
        self.assertEqual(options["DRMAllowList"].split(), ["i915", "radeon", "virtio_gpu"])
        self.assertIn('Driver "xrdpdev"', text)

    def test_documentation_points_at_the_examples(self):
        text = (ROOT / "docs" / "desktop-operations.md").read_text()
        for name in ("clean-stale-x-locks.sh", "xorgxrdp-gpu.conf"):
            self.assertIn(name, text)
            self.assertTrue((EXAMPLES / name).is_file())


if __name__ == "__main__":
    unittest.main()
