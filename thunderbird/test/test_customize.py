"""Unit tests of the consumer-side tools (consumer/customize.py, consumer/oauth_shared_mailbox.py).

Standard library only, no network, no Thunderbird: a stand-in installation (a directory with `thunderbird`,
an `omni.ja` zip and `application.ini`) is built in a temporary directory.
  python3 -m unittest discover -s thunderbird/test
The real thing (Mozilla's omni.ja, the add-on downloads, starting Thunderbird) is test/smoke.sh.
"""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

CONSUMER = Path(__file__).resolve().parent.parent / "consumer"
sys.path.insert(0, str(CONSUMER))
import customize  # noqa: E402
import oauth_shared_mailbox as oauth  # noqa: E402

SASL = '`user=${this._username}\\x01auth=Bearer ${retval}\\x01\\x01`'


def stand_in_module():
    """A module text that contains every context the OAuth patch expects, once (three times for one of them)."""
    parts = [old for old, _ in oauth.REPLACEMENTS]
    parts += ["login.username != this._username"] * 2
    parts.append(SASL)
    return "\n".join(parts) + "\n"


def make_app(root, module_text):
    app = Path(root) / "app"
    app.mkdir()
    (app / "thunderbird").write_text("#!/bin/sh\n")
    (app / "application.ini").write_text("[App]\nVersion=158.0.1\n")
    with zipfile.ZipFile(app / "omni.ja", "w") as archive:
        archive.writestr("modules/OAuth2Module.sys.mjs", module_text)
        archive.writestr("other.txt", "unchanged")
    return app


def xpi(manifest):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("background.js", "// code")
    return out.getvalue()


class CustomizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_tool(self, app, *args):
        return customize.main(["--app", str(app), *args])

    def test_oauth_patch_changes_only_the_module_and_marks_the_installation(self):
        app = make_app(self.root, stand_in_module())
        self.assertEqual(self.run_tool(app, "oauth-shared-mailbox"), 0)
        with zipfile.ZipFile(app / "omni.ja") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("other.txt"), b"unchanged")
            self.assertIn('server.getStringValue("oauth2.username")', archive.read("modules/OAuth2Module.sys.mjs").decode())
        self.assertIn("OAuth2Module", (self.root / "MODIFIED.txt").read_text())
        self.assertFalse(list(app.glob("*.malrunar-tmp")))

    def test_oauth_patch_refuses_a_module_it_does_not_know(self):
        app = make_app(self.root, "export function OAuth2Module() {}\n")
        original = (app / "omni.ja").read_bytes()
        self.assertEqual(self.run_tool(app, "oauth-shared-mailbox"), 1)
        self.assertEqual((app / "omni.ja").read_bytes(), original, "a failed patch must leave omni.ja as it was")
        self.assertFalse((self.root / "MODIFIED.txt").exists())

    def test_not_a_thunderbird_directory_is_reported(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(self.run_tool(empty, "verify"), 1)

    def test_policies_are_merged_not_replaced(self):
        app = make_app(self.root, "x")
        one, two = self.root / "one.json", self.root / "two.json"
        one.write_text(json.dumps({"policies": {"Preferences": {"a": 1}, "ExtensionSettings": {"x@y": {"v": 1}}}}))
        two.write_text(json.dumps({"policies": {"Preferences": {"b": 2}, "ExtensionSettings": {"z@y": {"v": 2}}}}))
        self.assertEqual(self.run_tool(app, "policies", str(one), str(two)), 0)
        merged = json.loads((app / "distribution" / "policies.json").read_text())["policies"]
        self.assertEqual(merged["Preferences"], {"a": 1, "b": 2})
        self.assertEqual(set(merged["ExtensionSettings"]), {"x@y", "z@y"})
        self.assertEqual(self.run_tool(app, "verify"), 0)

    def test_policy_fragment_without_policies_is_refused(self):
        app = make_app(self.root, "x")
        bad = self.root / "bad.json"
        bad.write_text("{}")
        self.assertEqual(self.run_tool(app, "policies", str(bad)), 1)
        self.assertFalse((app / "distribution").exists())

    def test_shipped_policy_fragments_are_valid(self):
        app = make_app(self.root, "x")
        fragments = sorted(str(p) for p in (CONSUMER / "policies").glob("*.json"))
        self.assertGreaterEqual(len(fragments), 2)
        self.assertEqual(self.run_tool(app, "policies", *fragments), 0)

    def test_manifest_rewrite_raises_only_the_compatibility_bound(self):
        for key in ("browser_specific_settings", "applications"):
            data = xpi({"name": "a", key: {"gecko": {"id": "a@b", "strict_max_version": "140.*"}}, "version": "1"})
            patched, addon_id, old = customize.rewrite_max_version(data, "158.*")
            self.assertEqual((addon_id, old), ("a@b", "140.*"))
            archive = zipfile.ZipFile(io.BytesIO(patched))
            self.assertEqual(archive.read("background.js"), b"// code")
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest[key]["gecko"]["strict_max_version"], "158.*")
            self.assertEqual(manifest["version"], "1")

    def test_manifest_without_gecko_block_fails_loudly(self):
        with self.assertRaises(customize.Failure):
            customize.rewrite_max_version(xpi({"name": "a"}), "158.*")

    def test_addon_needs_https_a_good_name_and_a_checksum(self):
        app = make_app(self.root, "x")
        digest = "0" * 64
        self.assertEqual(self.run_tool(app, "addon", "tbsync", "http://example.invalid/a.xpi", digest, "--dir", str(self.root / "d")), 1)
        self.assertEqual(self.run_tool(app, "addon", "Bad Name", "https://example.invalid/a.xpi", digest, "--dir", str(self.root / "d")), 1)
        self.assertEqual(self.run_tool(app, "addon", "tbsync", "https://example.invalid/a.xpi", "abc", "--dir", str(self.root / "d")), 1)
        self.assertFalse((self.root / "d").exists())

    def test_pinned_addons_are_complete(self):
        pins = json.loads((CONSUMER / "addons.json").read_text())
        self.assertEqual(set(pins), {"tbsync", "eas4tbsync"})
        for entry in pins.values():
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
        policy = json.loads((CONSUMER / "policies" / "tbsync.json").read_text())["policies"]["ExtensionSettings"]
        self.assertEqual({e["id"] for e in pins.values()}, set(policy))

    def test_default_max_version_follows_the_installed_major(self):
        app = make_app(self.root, "x")
        self.assertEqual(customize.installed_major(app), "158")


if __name__ == "__main__":
    unittest.main()
