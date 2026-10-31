"""Tests of the content hashes and of the pull request planning (standard library only).

  python3 -m unittest discover -s scripts/test

They run on copies of this checkout in temporary directories, so they check the real recipes: a
root pin bump must not change an image whose Dockerfile does not declare that pin.
"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import changed_images as ci  # noqa: E402
import malrunar_image as mi  # noqa: E402

IGNORE = shutil.ignore_patterns(".git", "__pycache__", "node_modules")
# Images that use no root pin at all, or only the one named in the test.
NO_ROOT_PINS = ("media-bridge", "work-browser", "session-kit", "node", "tail-browser-mcp", "thunderbird", "thunderbird-mcp", "msteams-mcp", "teams-service")


def edit_pin(root, key, value):
    path = root / "pins.env"
    text = path.read_text()
    new, count = re.subn(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    assert count == 1, key
    path.write_text(new)


class CopyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.base = self.tmp / "base"
        self.head = self.tmp / "head"
        shutil.copytree(mi.ROOT, self.base, ignore=IGNORE)
        shutil.copytree(mi.ROOT, self.head, ignore=IGNORE)

    def hashes(self, root):
        return {image: mi.image_hash(image, root=root) for image in mi.IMAGES}

    def differing(self):
        before, after = self.hashes(self.base), self.hashes(self.head)
        return {image for image in mi.IMAGES if before[image] != after[image]}


class PinScopeTest(CopyCase):
    def test_apt_snapshot_bump_republishes_only_images_that_use_it(self):
        edit_pin(self.head, "APT_SNAPSHOT", "20991231T000000Z")
        changed = self.differing()
        for image in NO_ROOT_PINS:
            self.assertNotIn(image, changed, f"{image} does not use APT_SNAPSHOT but its hash changed")
        for image in ("desktop-base", "obs-runtime", "obs-control", "guacd", "guacamole-tunnel", "office-convert"):
            self.assertIn(image, changed, f"{image} installs packages from the snapshot")
        self.assertEqual(set(ci.republished_images(self.base, self.head)), changed)

    def test_base_image_bump_does_not_touch_scripts_only_components(self):
        edit_pin(self.head, "UBUNTU_IMAGE", "ubuntu:26.04@sha256:" + "0" * 64)
        changed = self.differing()
        self.assertTrue({"media-bridge", "work-browser", "node", "tail-browser-mcp", "msteams-mcp", "teams-service"}.isdisjoint(changed))
        self.assertIn("desktop-base", changed)
        self.assertIn("obs-runtime", changed)  # through its parent

    def test_own_pins_still_count(self):
        pins = self.head / "node" / "pins.env"
        pins.write_text(pins.read_text().replace("NODE_VERSION=", "NODE_VERSION=9"))
        self.assertEqual(self.differing(), {"node"})

    def test_recipe_changes_count(self):
        readme = self.head / "media-bridge" / "README.md"
        readme.write_text(readme.read_text() + "\nmore\n")
        self.assertEqual(self.differing(), {"media-bridge"})

    def test_test_directory_does_not_change_the_hash(self):
        (self.head / "work-browser" / "test" / "extra.sh").write_text("#!/bin/sh\n")
        self.assertEqual(self.differing(), set())

    def test_legacy_hash_is_the_unscoped_one(self):
        edit_pin(self.head, "APT_SNAPSHOT", "20991231T000000Z")
        old = mi.image_hash("media-bridge", root=self.base, scoped=False)
        new = mi.image_hash("media-bridge", root=self.head, scoped=False)
        self.assertNotEqual(old, new)
        self.assertEqual(mi.image_hash("media-bridge", root=self.base), mi.image_hash("media-bridge", root=self.head))

    def test_declared_args(self):
        self.assertEqual(mi.declared_args("media-bridge", self.base), set())
        self.assertEqual(mi.declared_args("node", self.base), {"FETCH_IMAGE", "NODE_VERSION", "NODE_SHA256"})
        self.assertIn("APT_SNAPSHOT", mi.declared_args("desktop-base", self.base))

    def test_scoped_pins_passed_to_the_build(self):
        pins = mi.merged_pins("media-bridge", self.base)
        self.assertEqual(set(pins), {"VERSION"})
        self.assertIn("UBUNTU_IMAGE", mi.merged_pins("media-bridge", self.base, scoped=False))


class PlanTest(CopyCase):
    def run_plan(self, paths):
        return ci.changed_images(self.base, self.head, paths)

    def test_nothing_changed(self):
        self.assertEqual(self.run_plan([]), [])

    def test_test_only_change_retests_that_image(self):
        (self.head / "node" / "test" / "x").write_text("x")
        # the components that are tested with node are retested with it (malrunar_image.TESTED_WITH)
        self.assertEqual(self.run_plan(["node/test/x"]), ["node", "thunderbird-mcp", "msteams-mcp", "teams-service"])

    def test_components_follow_desktop_base(self):
        (self.head / "desktop-base" / "share" / "new").write_text("x")
        changed = self.run_plan(["desktop-base/share/new"])
        self.assertEqual(set(changed), {"desktop-base", "obs-runtime", "obs-control", "media-bridge", "work-browser", "session-kit", "thunderbird", "thunderbird-mcp", "msteams-mcp", "teams-service"})
        self.assertNotIn("node", changed)
        self.assertNotIn("guacd", changed)

    def test_pin_bump_retests_only_what_it_changes(self):
        edit_pin(self.head, "APT_SNAPSHOT", "20991231T000000Z")
        changed = set(self.run_plan(["pins.env"]))
        self.assertNotIn("tail-browser-mcp", changed)
        self.assertNotIn("node", changed)
        self.assertIn("guacd", changed)

    def test_new_image_is_changed(self):
        shutil.rmtree(self.base / "work-browser")
        self.assertIn("work-browser", self.run_plan(["work-browser/Dockerfile"]))

    def test_build_infrastructure_change_rebuilds_everything(self):
        self.assertEqual(self.run_plan(["scripts/build.sh"]), list(mi.IMAGES))
        self.assertEqual(self.run_plan([".github/workflows/pr.yml"]), list(mi.IMAGES))
        self.assertEqual(ci.changed_images(None, self.head, []), list(mi.IMAGES))

    def test_plan_outputs(self):
        out = ci.plan(["node", "work-browser", "obs-runtime"])
        self.assertEqual(json.loads(out["groups"]), ["node", "desktop-base obs-runtime obs-control"])
        self.assertEqual((out["work_browser"], out["media_bridge"], out["obs_control_e2e"]), ("true", "false", "true"))
        none = ci.plan([])
        self.assertEqual(json.loads(none["groups"]), [])
        self.assertEqual({none["work_browser"], none["media_bridge"], none["obs_control_e2e"]}, {"false"})

    def test_levels_keep_only_their_jobs(self):
        images = ["obs-control", "media-bridge", "node"]
        unit = ci.plan(images, "unit")
        self.assertEqual(json.loads(unit["groups"]), [])
        self.assertEqual({v for k, v in unit.items() if k not in ("images", "groups", "requested")}, {"false"})
        smoke = ci.plan(images, "smoke")
        self.assertEqual((smoke["media_bridge"], smoke["obs_control_e2e"]), ("true", "false"))
        self.assertEqual(json.loads(smoke["groups"]), ["node", "desktop-base obs-runtime obs-control"])
        e2e = ci.plan(images, "e2e")
        self.assertEqual((e2e["media_bridge"], e2e["obs_control_e2e"]), ("false", "true"))
        self.assertEqual(json.loads(e2e["groups"]), [])
        self.assertEqual(ci.plan(images, "all"), ci.plan(images))
        with self.assertRaises(ValueError):
            ci.plan(images, "everything")

    def test_every_image_is_in_a_build_group_or_has_a_job(self):
        grouped = {image for group in ci.BUILD_GROUPS for image in group}
        has_job = {"media-bridge", "work-browser", "session-kit", "thunderbird", "thunderbird-mcp", "msteams-mcp", "teams-service"}
        self.assertEqual(grouped | has_job, set(mi.IMAGES))
        self.assertFalse(grouped & has_job)

    def test_unchanged_parents_are_dependencies_not_requested_checks(self):
        out = ci.plan(["obs-control"])
        self.assertEqual(json.loads(out["groups"]), ["desktop-base obs-runtime obs-control"])
        self.assertEqual(out["requested"], "obs-control")
        changed_parent = ci.plan(["desktop-base", "obs-runtime", "obs-control"])
        self.assertEqual(set(changed_parent["requested"].split()), {"desktop-base", "obs-runtime", "obs-control"})


class RepositoryTest(unittest.TestCase):
    """The registry of images, the workflows and the image directories agree."""

    def test_publish_foundation_chain_and_matrix_cover_every_image(self):
        text = (mi.ROOT / ".github/workflows/publish.yml").read_text()
        listed = re.search(r"image: \[([^\]]*)\]", text).group(1).replace(" ", "").split(",")
        foundation_match = re.search(r"for image in ([a-z-]+(?: [a-z-]+)*); do", text)
        self.assertIsNotNone(foundation_match)
        foundation = foundation_match.group(1).split()
        self.assertEqual(set(listed) | set(foundation), set(mi.IMAGES))
        self.assertFalse(set(listed) & set(foundation))
        self.assertIn("needs: publish-foundation", text)
        for image, parent in mi.IMAGES.items():
            if parent:
                self.assertLess(foundation.index(parent), foundation.index(image))

    def test_weekly_bump_uses_repository_scoped_app_for_pr_trigger(self):
        text = (mi.ROOT / ".github/workflows/weekly.yml").read_text()
        self.assertIn("actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510", text)
        self.assertIn("repositories: malrunar", text)
        self.assertIn("permission-contents: write", text)
        self.assertIn("permission-pull-requests: write", text)
        self.assertIn("CI_APP_ID", text)
        self.assertIn("CI_APP_PRIVATE_KEY", text)

    def test_every_image_has_its_files(self):
        for image in mi.IMAGES:
            self.assertTrue((mi.ROOT / image / "Dockerfile").is_file(), image)
            self.assertIn("VERSION", mi.merged_pins(image), image)
            smoke = mi.ROOT / image / "test" / "smoke.sh"
            self.assertTrue(smoke.is_file() and smoke.stat().st_mode & 0o111, f"{image}/test/smoke.sh must be executable")

    def test_images_declare_the_pins_they_receive(self):
        root_pins = set(mi.parse_pins(mi.ROOT / "pins.env"))
        for image in mi.IMAGES:
            own = set(mi.parse_pins(mi.ROOT / image / "pins.env"))
            self.assertFalse(own & root_pins, image)


if __name__ == "__main__":
    unittest.main()
