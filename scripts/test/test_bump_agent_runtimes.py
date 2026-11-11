"""Offline tests of the fail-closed nightly Paseo/Codex/Claude pin updater."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bump_agent_runtimes as updater  # noqa: E402


OLD_DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64


class AgentRuntimeBumpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for image in ("paseo-runtime", "agent-dev-base"):
            (self.root / image).mkdir()
        (self.root / "paseo-runtime/pins.env").write_text(
            f"VERSION=0.14.0-malrunar1\n"
            f"PASEO_IMAGE=ghcr.io/getpaseo/paseo:0.14.0@{OLD_DIGEST}\n"
            "CODEX_VERSION=0.163.0\nCLAUDE_CODE_VERSION=2.1.302\n"
        )
        (self.root / "agent-dev-base/pins.env").write_text(
            "VERSION=0.1.0\nCODEX_VERSION=0.163.0\nCLAUDE_CODE_VERSION=2.1.302\n"
        )
        self.patch_root = patch.object(updater, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def versions(self):
        return updater.pins(self.root / "paseo-runtime/pins.env"), updater.pins(
            self.root / "agent-dev-base/pins.env"
        )

    def test_paseo_bump_updates_version_and_immutable_manifest_digest(self):
        with patch.object(updater, "paseo_latest", return_value="0.15.4"), patch.object(
            updater, "ghcr_paseo_digest", return_value=NEW_DIGEST
        ):
            updater.bump_paseo(self.root, False)
        paseo, _ = self.versions()
        self.assertEqual(paseo["VERSION"], "0.15.4-malrunar1")
        self.assertEqual(paseo["PASEO_IMAGE"], f"ghcr.io/getpaseo/paseo:0.15.4@{NEW_DIGEST}")

    def test_both_images_receive_matching_cli_pins(self):
        versions = {"@openai/codex": "0.167.1", "@anthropic-ai/claude-code": "2.1.322"}
        with patch.object(updater, "npm_latest", side_effect=versions.__getitem__):
            updater.bump_clis(self.root, False)
        for pins in self.versions():
            self.assertEqual(pins["CODEX_VERSION"], "0.167.1")
            self.assertEqual(pins["CLAUDE_CODE_VERSION"], "2.1.322")

    def test_dry_run_never_writes(self):
        original = (self.root / "paseo-runtime/pins.env").read_text()
        with patch.object(updater, "paseo_latest", return_value="0.15.4"), patch.object(
            updater, "ghcr_paseo_digest", return_value=NEW_DIGEST
        ):
            updater.bump_paseo(self.root, True)
        self.assertEqual((self.root / "paseo-runtime/pins.env").read_text(), original)

    def test_refuses_retagged_existing_upstream_release(self):
        with patch.object(updater, "paseo_latest", return_value="0.14.0"), patch.object(
            updater, "ghcr_paseo_digest", return_value=NEW_DIGEST
        ):
            with self.assertRaisesRegex(ValueError, "different digest"):
                updater.bump_paseo(self.root, False)

    def test_refuses_paseo_or_cli_downgrades(self):
        with patch.object(updater, "paseo_latest", return_value="0.13.9"):
            with self.assertRaisesRegex(ValueError, "downgrade Paseo"):
                updater.bump_paseo(self.root, False)
        with patch.object(updater, "npm_latest", side_effect=lambda name: "0.162.0" if name == "@openai/codex" else "2.1.322"):
            with self.assertRaisesRegex(ValueError, "refusing to downgrade"):
                updater.bump_clis(self.root, False)

    def test_release_and_npm_metadata_validation(self):
        with patch.object(updater, "json_url", return_value={
            "tag_name": "v0.16.0-beta.1", "prerelease": True, "draft": False,
        }):
            with self.assertRaisesRegex(ValueError, "not stable"):
                updater.paseo_latest()
        with patch.object(updater, "json_url", return_value={
            "name": "@openai/codex", "version": "0.168.0-beta.1",
        }):
            with self.assertRaisesRegex(ValueError, "expected a stable"):
                updater.npm_latest("@openai/codex")


if __name__ == "__main__":
    unittest.main()
