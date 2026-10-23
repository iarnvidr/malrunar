"""Forward-auth identity module (tools/identity): the vendored copies are in sync and the shared test
vectors pass in Python and Node. Run by the existing `python3 -m unittest discover -s scripts/test` step,
so the `scripts` job covers it without a change to pr.yml (a pr.yml change would rebuild every image).
Standard library only."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IDENTITY = ROOT / "tools" / "identity"


def run(*command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


class IdentityTests(unittest.TestCase):
    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_vendored_copies_are_in_sync(self):
        self.assertOk(run("bash", "scripts/sync-identity.sh", "--check"))

    def test_python_module_and_vectors(self):
        self.assertOk(run("python3", "-I", "-m", "unittest", "discover", "-s", str(IDENTITY)))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node_module_and_vectors(self):
        self.assertOk(run("node", "--check", str(IDENTITY / "forward-auth.mjs")))
        self.assertOk(run("node", "--test", str(IDENTITY / "forward-auth.test.mjs")))


if __name__ == "__main__":
    unittest.main()
