"""Offline source/CI contracts; no build, package install or registry access."""
import importlib.util
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import malrunar_image as images
import changed_images as planner


class PackagingTests(unittest.TestCase):
    def test_component_routes_through_existing_hash_build_and_public_ci(self):
        self.assertIn("devops-service", images.IMAGES)
        self.assertIsNone(images.IMAGES["devops-service"])
        self.assertEqual(planner.plan(["devops-service"], "smoke")["groups"], '["devops-service"]')
        self.assertEqual(planner.plan(["devops-service"], "unit")["groups"], "[]")
        self.assertIn("devops-service", (ROOT / ".github/workflows/publish.yml").read_text())
        self.assertIn("unittest discover -s devops-service/test", (ROOT / ".github/workflows/pr.yml").read_text())

    def test_private_site_changes_do_not_change_public_foundation_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "devops-service", root / "devops-service", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(ROOT / "pins.env", root / "pins.env")
            before = images.image_hash("devops-service", root=root)
            (root / "private-site-policy.json").write_text('{"denyUnknown": true}')
            self.assertEqual(images.image_hash("devops-service", root=root), before)
            pin = root / "devops-service/pins.env"
            pin.write_text(re.sub(r"NODE_IMAGE=.*", "NODE_IMAGE=example.invalid/node@sha256:" + "0" * 64, pin.read_text()))
            self.assertNotEqual(images.image_hash("devops-service", root=root), before)

    def test_native_venv_stays_with_its_base_and_node_is_external_immutable_input(self):
        recipe = (ROOT / "devops-service/Dockerfile").read_text()
        pins = images.merged_pins("devops-service", root=ROOT)
        self.assertRegex(pins["NODE_IMAGE"], r"^ghcr\.io/[a-z0-9-]+/node@sha256:[a-f0-9]{64}$")
        self.assertIn("FROM runtime AS dependencies", recipe)
        self.assertIn("FROM runtime AS service", recipe)
        self.assertNotRegex(recipe, r"COPY[^\n]*/usr/lib")
        self.assertNotIn("curl", recipe)
        self.assertIn("UNPUBLISHED", (ROOT / "devops-service/SOURCE.txt").read_text())

    def test_python_lock_has_exact_hashed_transitives_and_preserves_direct_pins(self):
        text = (ROOT / "devops-service/requirements.lock").read_text()
        blocks = re.split(r"\n(?=[a-zA-Z0-9])", text)
        packages = {}
        for block in blocks:
            match = re.search(r"^([a-zA-Z0-9_.-]+)==([^\s\\]+)", block, re.MULTILINE)
            if not match:
                continue
            self.assertNotIn(match[1], packages)
            packages[match[1]] = match[2]
            self.assertRegex(block, r"--hash=sha256:[a-f0-9]{64}")
            self.assertNotRegex(block, r"https?://|--extra-index|--trusted-host")
        self.assertGreater(len(packages), 100)
        for line in (ROOT / "devops-service/requirements.txt").read_text().splitlines():
            name, version = line.split("==")
            self.assertEqual(packages[name], version)
        recipe = (ROOT / "devops-service/Dockerfile").read_text()
        for required in ("--require-hashes", "--only-binary=:all:", "-r requirements.lock", "venv/bin/pip check"):
            self.assertIn(required, recipe)
        self.assertNotIn("-r requirements.txt", recipe)

    def test_snapshot_bootstrap_uses_existing_certificate_and_signature_checks(self):
        helper = ROOT / "devops-service/scripts/apt-snapshot.sh"
        self.assertEqual(helper.read_bytes(), (ROOT / "office-convert/scripts/apt-snapshot.sh").read_bytes())
        recipe = (ROOT / "devops-service/Dockerfile").read_text()
        self.assertIn("malrunar_snapshot_enable", recipe)
        self.assertNotIn("Verify-Peer=false", recipe)


if __name__ == "__main__":
    unittest.main()
