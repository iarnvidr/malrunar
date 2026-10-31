"""Fault-injection tests for the exact-tested-image consumer.

These tests replace GitHub and Docker at the process boundary. They prove that
bad producer facts, source hashes, or OCI config identities stop before Docker
loads or runs an image.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import load_tested_images as loader


class LoadTestedImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.event = self.root / "event.json"
        self.event.write_text("{}\n")
        self.preflight = self.root / "canonical-preflight.json"
        self.preflight.write_text(json.dumps({"registry": "fixture", "cached": [], "missing": []}))
        self.output = self.root / "loaded-images.json"
        self.config = "sha256:" + "c" * 64
        self.manifest = "sha256:" + "d" * 64
        self.archive_sha = "e" * 64
        self.head = "a" * 40
        self.base = "b" * 40
        self.tree = "f" * 40
        self.image = "guacd"
        self.args = argparse.Namespace(
            root=self.root, targets=[self.image], run_id=987654, run_attempt=2,
            pr_event=self.event, tree=self.tree, preflight_json=self.preflight,
            output_manifest=self.output)

    def candidate_entry(self, candidate_dir, **overrides):
        archive = candidate_dir / f"{self.image}.oci.tar"
        archive.write_bytes(b"candidate OCI archive fixture")
        entry = {
            "image": self.image, "source": "candidate",
            "content_hash": "1" * 64, "version": "7.2.0",
            "config_digest": self.config, "manifest_digest": self.manifest,
            "root_digest": "sha256:" + "3" * 64, "archive_sha256": self.archive_sha,
            "archive_path": str(archive),
            "artifact_id": 123456,
            "artifact_digest": "sha256:" + "2" * 64,
            "artifact_name": f"malrunar-image-candidates-987654-2-build-targets--{self.image}",
            "job_key": f"build-targets--{self.image}", "producer_job": f"build ({self.image})",
            "producer_run_id": self.args.run_id,
            "producer_run_attempt": self.args.run_attempt,
            "head_sha": self.head, "base_sha": self.base,
            "tested_tree": self.tree, "pr_number": 24,
        }
        entry.update(overrides)
        return entry

    def fake_resolver(self, entry, calls):
        def resolve(root, targets, run_id, run_attempt, pr_event_path, tested_tree,
                    preflight_json, *, candidate_dir, current_run_in_progress):
            calls.append({"targets": targets, "run_id": run_id, "attempt": run_attempt,
                          "tree": tested_tree, "in_progress": current_run_in_progress})
            if entry["source"] == "candidate":
                entry["archive_path"] = str(candidate_dir / f"{entry['image']}.oci.tar")
                Path(entry["archive_path"]).write_bytes(b"candidate OCI archive fixture")
            return [entry]
        return resolve

    def test_verified_candidate_loads_exact_archive_and_writes_artifact_mapping(self):
        entry = self.candidate_entry(Path(self.tmp.name))
        calls = []
        identity = {"archive_sha256": self.archive_sha, "manifest_digest": self.manifest,
                    "config_digest": self.config, "publishable_root_digest": "sha256:" + "3" * 64}
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value=entry["content_hash"]), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.candidate, "oci_identity", return_value=identity), \
             mock.patch.object(loader.candidate, "image_id", return_value=self.config), \
             mock.patch.object(loader.subprocess, "run") as run:
            result = loader.load_tested_images(self.args)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:6], ["skopeo", "copy", "--override-os", "linux",
                                       "--override-arch", "amd64"])
        self.assertTrue(command[6].startswith("oci-archive:"))
        self.assertEqual(command[7], f"docker-daemon:malrunar/{self.image}:h-{entry['content_hash']}")
        self.assertEqual(calls[0]["targets"], [self.image])
        self.assertEqual(result["source_run_id"], self.args.run_id)
        self.assertEqual(result["images"][0]["artifact_id"], 123456)
        self.assertEqual(result["images"][0]["artifact_digest"], entry["artifact_digest"])
        self.assertEqual(json.loads(self.output.read_text()), result)

    def test_wrong_producer_run_fails_before_any_docker_operation(self):
        entry = self.candidate_entry(Path(self.tmp.name), producer_run_id=self.args.run_id + 1)
        calls = []
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value=entry["content_hash"]), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.candidate, "oci_identity") as identity, \
             mock.patch.object(loader.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "another producer run attempt"):
                loader.load_tested_images(self.args)
        identity.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_source_hash_fails_before_any_docker_operation(self):
        entry = self.candidate_entry(Path(self.tmp.name), content_hash="9" * 64)
        calls = []
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value="1" * 64), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.candidate, "oci_identity") as identity, \
             mock.patch.object(loader.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "content hash differs"):
                loader.load_tested_images(self.args)
        identity.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_archive_config_fails_before_any_docker_operation(self):
        entry = self.candidate_entry(Path(self.tmp.name))
        calls = []
        identity = {"archive_sha256": self.archive_sha, "manifest_digest": self.manifest,
                    "config_digest": "sha256:" + "9" * 64,
                    "publishable_root_digest": "sha256:" + "3" * 64}
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value=entry["content_hash"]), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.candidate, "oci_identity", return_value=identity), \
             mock.patch.object(loader.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "config_digest differs"):
                loader.load_tested_images(self.args)
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_digest_pinned_canonical_dependency_is_pulled_and_config_checked(self):
        image = "desktop-base"
        self.args.targets = [image]
        content_hash = "1" * 64
        root_digest = "sha256:" + "4" * 64
        reference = f"{loader.candidate.IMAGE_REGISTRY}/{image}@{root_digest}"
        entry = {
            "image": image, "source": "canonical", "content_hash": content_hash,
            "version": "7.2.0", "config_digest": self.config,
            "root_digest": root_digest, "reference": reference,
            "producer_run_id": self.args.run_id, "producer_run_attempt": self.args.run_attempt,
            "head_sha": self.head, "base_sha": self.base, "tested_tree": self.tree,
            "pr_number": 24,
        }
        calls = []
        events = []
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value=content_hash), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.candidate, "image_id",
                                side_effect=lambda ref: events.append(("verify", ref)) or self.config), \
             mock.patch.object(loader.subprocess, "run",
                                side_effect=lambda command, check: events.append(("run", command))) as run:
            result = loader.load_tested_images(self.args)
        local_reference = f"malrunar/{image}:h-{content_hash}"
        self.assertEqual(run.call_args_list, [
            mock.call(["docker", "pull", "--platform", "linux/amd64", reference], check=True),
            mock.call(["docker", "tag", reference, local_reference], check=True),
        ])
        self.assertEqual(events, [
            ("run", ["docker", "pull", "--platform", "linux/amd64", reference]),
            ("verify", reference),
            ("run", ["docker", "tag", reference, local_reference]),
        ])
        self.assertEqual(result["images"][0]["source"], "canonical")
        self.assertEqual(result["images"][0]["root_digest"], root_digest)
        self.assertEqual(result["images"][0]["local_reference"], local_reference)

    def test_wrong_canonical_digest_fails_before_pull(self):
        image = "desktop-base"
        self.args.targets = [image]
        content_hash = "1" * 64
        root_digest = "sha256:" + "4" * 64
        entry = {
            "image": image, "source": "canonical", "content_hash": content_hash,
            "version": "7.2.0", "config_digest": self.config,
            "root_digest": root_digest,
            "reference": f"{loader.candidate.IMAGE_REGISTRY}/{image}@sha256:{'5' * 64}",
            "producer_run_id": self.args.run_id, "producer_run_attempt": self.args.run_attempt,
            "head_sha": self.head, "base_sha": self.base, "tested_tree": self.tree,
            "pr_number": 24,
        }
        calls = []
        with mock.patch.object(loader.candidate, "resolve_tested_images",
                               side_effect=self.fake_resolver(entry, calls), create=True), \
             mock.patch.object(loader.images, "image_hash", return_value=content_hash), \
             mock.patch.object(loader.images, "version", return_value=entry["version"]), \
             mock.patch.object(loader.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not pinned to its verified root digest"):
                loader.load_tested_images(self.args)
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_in_progress_exception_is_limited_to_the_current_pr_attempt(self):
        variables = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ID": str(self.args.run_id),
                     "GITHUB_RUN_ATTEMPT": str(self.args.run_attempt)}
        with mock.patch.dict(os.environ, variables, clear=False):
            self.assertTrue(loader.current_run_in_progress(self.args.run_id, self.args.run_attempt))
            self.assertFalse(loader.current_run_in_progress(self.args.run_id + 1, self.args.run_attempt))
            self.assertFalse(loader.current_run_in_progress(self.args.run_id, self.args.run_attempt + 1))
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_run"}, clear=False):
                self.assertFalse(loader.current_run_in_progress(self.args.run_id, self.args.run_attempt))


if __name__ == "__main__":
    unittest.main()
