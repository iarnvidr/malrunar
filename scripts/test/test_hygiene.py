"""Tests of scripts/check-public-hygiene.sh on throwaway git repositories (standard library only).

The check must pass on this repository, reject the SHAPES of real tenant data (GUIDs, mail addresses,
tenant host names, tokens, chat ids) and accept the synthetic values the fixtures use. The bad samples
below are assembled from pieces so this file does not trip the check itself.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "check-public-hygiene.sh"


def run_check(files):
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "scripts").mkdir()
        shutil.copy(SCRIPT, tmp / "scripts" / "check-public-hygiene.sh")
        for name, text in files.items():
            (tmp / name).write_text(text)
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp), "GIT_CONFIG_GLOBAL": "/dev/null"}
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True, env=env)
        result = subprocess.run(["bash", "scripts/check-public-hygiene.sh"], cwd=tmp, env=env, capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr
    finally:
        shutil.rmtree(tmp, True)


REAL_GUID = "3f6a" + "92c1-7b4e-4d0a-9c53-" + "a1b2c3d4e5f6"
BAD = {
    "a real-looking GUID": ("fixture.json", f'{{"id": "{REAL_GUID}"}}\n'),
    "a mail address on a real domain": ("fixture.json", '{"mail": "someone' + '@' + 'gmail' + '.com"}\n'),
    "a tenant host name": ("fixture.json", "https://" + "acme" + ".sharepoint" + ".com/sites/x\n"),
    "a tenant host name (onmicrosoft)": ("fixture.json", "acme" + ".onmicrosoft" + ".com\n"),
    "a JWT": ("fixture.json", "token eyJ" + "hbGciOiJub25lIn0" + "." + "eyJzdWIiOiJ4eHh4eHh4eHgifQ" + ".sig\n"),
    "a Teams chat id": ("fixture.json", "19:" + "0123456789abcdef0123456789abcdef" + "@thread.v2\n"),
    "a tailnet name": ("notes.md", "ssh host" + ".tail1234" + ".ts" + ".net\n"),
}
GOOD = (
    ("synthetic GUIDs", "11111111-1111-1111-1111-111111111111 aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 00000001-0000-0000-0000-000000000000\n"),
    ("public application ids", "5e3ce6c0-2b1f-4285-8d4b-75ee78787346 1fec8e78-bce4-4aaf-ab1b-5451cc387264 00000003-0000-0000-c000-000000000000\n"),
    ("example mail addresses", "user" + "@example.invalid, a" + "@b.example.org, c" + "@local.invalid, d" + "@example.com\n"),
    ("file names with @2x and Teams id suffixes", "icon@2x.png 19:abc@thread.v2 19:x_y@unq.gbl.spaces\n"),
    ("the fictional tenant", "https://contoso.sharepoint.com/sites/docs\n"),
)


class HygieneTest(unittest.TestCase):
    def test_this_repository_passes(self):
        status = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

    def test_bad_samples_are_rejected(self):
        for label, (name, text) in BAD.items():
            with self.subTest(label):
                code, output = run_check({name: text})
                self.assertEqual(code, 1, f"{label} was accepted:\n{output}")

    def test_synthetic_samples_are_accepted(self):
        for label, text in GOOD:
            with self.subTest(label):
                code, output = run_check({"fixture.txt": text})
                self.assertEqual(code, 0, f"{label} was rejected:\n{output}")


if __name__ == "__main__":
    unittest.main()
