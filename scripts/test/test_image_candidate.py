"""Focused checks for exact PR-tested image candidate evidence."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import image_candidate as candidate  # noqa: E402
import malrunar_image as images  # noqa: E402


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layout(path, predicates=("https://spdx.dev/Document", "https://slsa.dev/provenance/v1")):
    config = b'{"architecture":"amd64","os":"linux"}'
    layer = b"layer-bytes"
    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": digest(config)},
                           "layers": [{"digest": digest(layer)}]}, separators=(",", ":")).encode()
    manifest_digest = digest(manifest)
    files = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "blobs/sha256/" + digest(config)[7:]: config,
        "blobs/sha256/" + digest(layer)[7:]: layer,
        "blobs/sha256/" + manifest_digest[7:]: manifest,
    }
    descriptors = [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest, "size": len(manifest),
                    "platform": {"os": "linux", "architecture": "amd64"}}]
    for predicate in predicates:
        statement = json.dumps({"_type": "https://in-toto.io/Statement/v1",
                                "subject": [{"digest": {"sha256": manifest_digest[7:]}}],
                                "predicateType": predicate, "predicate": {}},
                               separators=(",", ":")).encode()
        statement_digest = digest(statement)
        attestation = json.dumps({"schemaVersion": 2, "layers": [{
            "digest": statement_digest,
            "annotations": {"in-toto.io/predicate-type": predicate},
        }]}, separators=(",", ":")).encode()
        attestation_digest = digest(attestation)
        files["blobs/sha256/" + statement_digest[7:]] = statement
        files["blobs/sha256/" + attestation_digest[7:]] = attestation
        descriptors.append({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": attestation_digest, "size": len(attestation),
                            "annotations": {"vnd.docker.reference.type": "attestation-manifest"}})
    files["index.json"] = json.dumps({"schemaVersion": 2, "manifests": descriptors},
                                     separators=(",", ":")).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return manifest_digest, digest(config), digest(files["index.json"])


class CandidateSelectionTests(unittest.TestCase):
    def test_selection_is_intersection_in_canonical_order(self):
        self.assertEqual(candidate.select(["node", "desktop-base"], "node desktop-base guacd"),
                         ["desktop-base", "node"])

    def test_unknown_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown image"):
            candidate.select(["made-up"], "desktop-base")


class OciIdentityTests(unittest.TestCase):
    def test_checks_archive_and_both_buildkit_attestations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            manifest, config, index = layout(path)
            identity = candidate.oci_identity(path)
        self.assertEqual(identity["manifest_digest"], manifest)
        self.assertEqual(identity["config_digest"], config)
        self.assertEqual(identity["index_digest"], index)
        self.assertEqual(set(identity["attestation_predicates"]),
                         {"https://spdx.dev/Document", "https://slsa.dev/provenance/v1"})

    def test_requires_both_sbom_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            layout(path, ("https://spdx.dev/Document",))
            with self.assertRaisesRegex(ValueError, "provenance"):
                candidate.oci_identity(path)


class ReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 123, "run_attempt": 2, "pull_requests": [{"base": {"sha": "b" * 40}}]}
        self.pr = {"head": {"sha": "a" * 40}, "number": 7}
        self.receipt = {"schema": 1, "repository": candidate.REPOSITORY,
                        "workflow_path": candidate.PR_WORKFLOW, "run_id": 123,
                        "run_attempt": 2, "event": "pull_request", "head_sha": "a" * 40,
                        "base_sha": "b" * 40, "pr_number": 7, "tested_tree": "c" * 40}

    def test_receipt_must_bind_exact_run_head_base_and_tree(self):
        self.assertEqual(candidate.verify_run_receipt(self.receipt, self.run, candidate.PR_WORKFLOW,
                                                      self.pr, "c" * 40), "b" * 40)
        for field, value in (("run_attempt", 1), ("head_sha", "d" * 40),
                             ("base_sha", "e" * 40), ("tested_tree", "f" * 40)):
            changed = dict(self.receipt, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not identify"):
                candidate.verify_run_receipt(changed, self.run, candidate.PR_WORKFLOW,
                                              self.pr, "c" * 40)

    @mock.patch.object(candidate, "api")
    def test_policy_files_compare_to_trusted_run_base(self, github_api):
        github_api.side_effect = [{"sha": "same"}, {"sha": "same"}]
        candidate.trusted_blobs([candidate.PR_WORKFLOW], "b" * 40, "a" * 40)
        self.assertIn("?ref=" + "b" * 40, github_api.call_args_list[0].args[0])
        self.assertIn("?ref=" + "a" * 40, github_api.call_args_list[1].args[0])

    @mock.patch.object(candidate, "api")
    def test_policy_changes_in_candidate_workflow_are_rejected(self, github_api):
        github_api.side_effect = [{"sha": "trusted"}, {"sha": "changed"}]
        with self.assertRaisesRegex(ValueError, "differs from accepted main"):
            candidate.trusted_blobs([candidate.PR_WORKFLOW], "b" * 40, "a" * 40)

    def test_expected_receipt_workflow_is_fixed(self):
        with self.assertRaisesRegex(ValueError, "does not identify"):
            candidate.verify_run_receipt(self.receipt, self.run, candidate.QUICKSTART_WORKFLOW,
                                         self.pr, "c" * 40)


if __name__ == "__main__":
    unittest.main()
