"""Focused checks for exact PR-tested image candidate evidence."""
import hashlib
import io
import json
import os
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


def layout(path, predicates=("https://spdx.dev/Document", "https://slsa.dev/provenance/v1"),
           duplicate=False, nested=False):
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
    if duplicate:
        descriptors.append(dict(descriptors[0]))
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
    inner_index = json.dumps({"schemaVersion": 2, "manifests": descriptors},
                             separators=(",", ":")).encode()
    if nested:
        nested_digest = digest(inner_index)
        files["blobs/sha256/" + nested_digest[7:]] = inner_index
        files["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [{
            "mediaType": "application/vnd.oci.image.index.v1+json", "digest": nested_digest,
            "size": len(inner_index), "platform": {"os": "linux", "architecture": "amd64"},
        }]}, separators=(",", ":")).encode()
    else:
        files["index.json"] = inner_index
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return manifest_digest, digest(config), digest(files["index.json"])


class GitHubRedirectTests(unittest.TestCase):
    def test_cross_host_artifact_redirect_drops_authorization(self):
        request = candidate.urllib.request.Request(
            "https://api.github.com/repos/iarnvidr/malrunar/actions/artifacts/123/zip",
            headers={"Authorization": "Bearer secret", "Proxy-Authorization": "Basic secret"},
        )
        redirected = candidate.GitHubRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://objects.example.invalid/archive?sig=secret"
        )
        self.assertNotIn("Authorization", redirected.headers)
        self.assertNotIn("Proxy-authorization", redirected.headers)

    def test_http_error_hides_signed_query(self):
        request = candidate.urllib.request.Request("https://api.github.com/repos/example/repo")
        error = candidate.urllib.error.HTTPError(
            "https://objects.example.invalid/archive?sig=secret", 401, "Unauthorized", {}, io.BytesIO()
        )
        with mock.patch.object(candidate.urllib.request.OpenerDirector, "open", side_effect=error):
            with self.assertRaisesRegex(ValueError, r"objects\.example\.invalid/archive") as caught:
                candidate.open_github_request(request, timeout=1)
        self.assertNotIn("sig=secret", str(caught.exception))

    def test_redirect_rejects_plain_http(self):
        request = candidate.urllib.request.Request("https://api.github.com/a")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            candidate.GitHubRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "http://objects.example.invalid/archive"
            )

    def test_initial_request_is_limited_to_github_api_https_get(self):
        request = candidate.urllib.request.Request("https://objects.example.invalid/archive")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            candidate.open_github_request(request, timeout=1)


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

    def test_duplicate_platform_descriptor_is_one_image_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            manifest, _, _ = layout(path, duplicate=True)
            self.assertEqual(candidate.oci_identity(path)["manifest_digest"], manifest)

    def test_resolves_image_manifest_nested_under_exported_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            manifest, _, _ = layout(path, nested=True)
            identity = candidate.oci_identity(path)
            self.assertEqual(identity["manifest_digest"], manifest)
            with tarfile.open(path) as archive:
                outer = json.load(archive.extractfile("index.json"))
            root_digest = outer["manifests"][0]["digest"]
            self.assertNotEqual(identity["index_digest"], root_digest)
            self.assertEqual(identity["publishable_root_digest"], root_digest)


class DockerSavedConfigTests(unittest.TestCase):
    def test_image_identity_uses_saved_config_digest_not_inspect_descriptor(self):
        inspect_descriptor = "sha256:" + "b" * 64
        config = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
        with mock.patch.object(candidate, "_docker_saved_config",
                                return_value=(inspect_descriptor, config, "blobs/sha256/config")):
            self.assertEqual(candidate.image_id("malrunar/node:test"), digest(config))

    def test_reads_legacy_and_oci_layout_config_members_without_saving_layers(self):
        image = "sha256:" + "a" * 64
        config = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'

        class Process:
            def __init__(self, archive):
                self.stdout = io.BytesIO(archive)

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                raise AssertionError("completed docker save should not be terminated")

            def kill(self):
                raise AssertionError("completed docker save should not be killed")

        for member_name in ("a" * 64 + ".json", "blobs/sha256/" + "a" * 64):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:") as archive:
                member = tarfile.TarInfo(member_name)
                member.size = len(config)
                archive.addfile(member, io.BytesIO(config))
            with self.subTest(member_name=member_name), \
                    mock.patch.object(candidate, "_docker_inspect_id", return_value=image), \
                    mock.patch.object(candidate.subprocess, "Popen", return_value=Process(stream.getvalue())):
                self.assertEqual(candidate._docker_saved_config("malrunar/node:test"),
                                 (image, config, member_name))


class ReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 123, "run_attempt": 2, "head_sha": "a" * 40,
                    "event": "pull_request", "path": candidate.PR_WORKFLOW,
                    "conclusion": "success", "pull_requests": []}
        self.pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}, "number": 7}
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
    def test_successful_merged_pr_run_does_not_need_pull_request_association(self, github_api):
        run = dict(self.run, repository={"full_name": candidate.REPOSITORY},
                   head_repository={"full_name": candidate.REPOSITORY})
        github_api.return_value = {"workflow_runs": [run]}
        self.assertEqual(candidate.successful_run(candidate.PR_WORKFLOW, self.pr), run)

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

    def test_docs_only_plan_binds_empty_selection_and_all_image_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            output = Path(directory) / "image-plan.json"
            event_path.write_text(json.dumps({"pull_request": {
                "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}, "number": 7,
            }}))
            env = {
                "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": candidate.REPOSITORY, "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_SHA": "d" * 40,
                "MALRUNAR_PLAN_IMAGES": "",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(candidate, "git", return_value="c" * 40):
                candidate.record_plan(images.ROOT, output)
            receipt = json.loads(output.read_text())
            base = candidate.validate_plan_receipt(receipt, self.run, self.pr, "c" * 40, images.ROOT)
        self.assertEqual(base, "b" * 40)
        self.assertEqual(receipt["selected_images"], [])
        self.assertEqual(set(receipt["content_hashes"]), set(images.IMAGES))
        self.assertEqual(set(receipt["versions"]), set(images.IMAGES))

    def test_changed_plan_selects_image_and_rejects_stale_hash(self):
        receipt = {
            "schema": 1, "repository": candidate.REPOSITORY, "workflow_path": candidate.PR_WORKFLOW,
            "run_id": 123, "run_attempt": 2, "event": "pull_request", "workflow_sha": "d" * 40,
            "head_sha": "a" * 40, "base_sha": "b" * 40, "pr_number": 7, "tested_tree": "c" * 40,
            "selected_images": ["desktop-base"],
            "content_hashes": {image: images.image_hash(image) for image in images.IMAGES},
            "versions": {image: images.version(image) for image in images.IMAGES},
        }
        candidate.validate_plan_receipt(receipt, self.run, self.pr, "c" * 40, images.ROOT)
        self.assertIn("desktop-base", receipt["selected_images"])
        stale = dict(receipt, content_hashes=dict(receipt["content_hashes"]))
        stale["content_hashes"]["desktop-base"] = "0" * 16
        with self.assertRaisesRegex(ValueError, "content hash differs from accepted main"):
            candidate.validate_plan_receipt(stale, self.run, self.pr, "c" * 40, images.ROOT)

    def test_producer_change_plan_selecting_every_image_passes_bootstrap_validation(self):
        receipt = {
            "schema": 1, "repository": candidate.REPOSITORY, "workflow_path": candidate.PR_WORKFLOW,
            "run_id": 123, "run_attempt": 2, "event": "pull_request", "workflow_sha": "d" * 40,
            "head_sha": "a" * 40, "base_sha": "b" * 40, "pr_number": 7, "tested_tree": "c" * 40,
            "selected_images": list(images.IMAGES),
            "content_hashes": {image: images.image_hash(image) for image in images.IMAGES},
            "versions": {image: images.version(image) for image in images.IMAGES},
        }
        self.assertEqual(candidate.validate_plan_receipt(receipt, self.run, self.pr, "c" * 40, images.ROOT),
                         "b" * 40)
        self.assertEqual(receipt["selected_images"], list(images.IMAGES))


class RetrievePlanSelectionTests(unittest.TestCase):
    def setUp(self):
        self.image = "desktop-base"
        self.merge_sha = "e" * 40
        self.merge_tree = "f" * 40
        self.run = {"id": 123, "run_attempt": 2}
        self.pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}, "number": 7}
        self.plan_metadata = {"id": 55, "digest": "sha256:" + "1" * 64}
        self.plan = {
            "schema": 1, "repository": candidate.REPOSITORY, "workflow_path": candidate.PR_WORKFLOW,
            "run_id": 123, "run_attempt": 2, "event": "pull_request", "workflow_sha": "d" * 40,
            "head_sha": "a" * 40, "base_sha": "b" * 40, "pr_number": 7,
            "tested_tree": self.merge_tree, "selected_images": [],
            "content_hashes": {image: images.image_hash(image) for image in images.IMAGES},
            "versions": {image: images.version(image) for image in images.IMAGES},
        }

    def _retrieve(self, selected_images, output):
        plan = dict(self.plan, selected_images=selected_images)
        with mock.patch.object(candidate, "git", side_effect=[self.merge_sha, self.merge_tree]), \
                mock.patch.object(candidate, "exact_pr", return_value=self.pr), \
                mock.patch.object(candidate, "successful_run", return_value=self.run), \
                mock.patch.object(candidate, "plan_artifact", return_value=self.plan_metadata), \
                mock.patch.object(candidate, "download_artifact", return_value=Path("/tmp/plan.zip")), \
                mock.patch.object(candidate, "verify_plan_artifact", return_value=(plan, "b" * 40)), \
                mock.patch.object(candidate, "candidate_artifacts", return_value=[]), \
                mock.patch.object(candidate, "verify_quickstart", return_value={"run_id": 124}):
            return candidate.retrieve(images.ROOT, self.image, self.merge_sha, output)

    def test_docs_only_plan_creates_bound_no_change_mapping_without_image_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "accepted"
            self.assertIsNone(self._retrieve([], output))
            accepted = json.loads((output / "accepted-image.json").read_text())
            self.assertIsNone(accepted["candidate"])
            self.assertEqual(accepted["no_change"]["image"], self.image)
            self.assertEqual(accepted["no_change"]["content_hash"], self.plan["content_hashes"][self.image])
            self.assertEqual(accepted["plan"]["artifact_id"], self.plan_metadata["id"])
            self.assertFalse((output / "image.oci.tar").exists())

    def test_selected_image_without_exact_candidate_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "expected one exact PR-tested image artifact"):
                self._retrieve([self.image], Path(directory) / "accepted")


class CandidatePromotionTests(unittest.TestCase):
    def test_unchanged_image_requires_no_change_plan_binding_and_reuses_verified_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = "desktop-base"
            content_hash = images.image_hash(image)
            version = images.version(image)
            registry_digest = "sha256:" + "1" * 64
            accepted = {
                "schema": 1, "repository": candidate.REPOSITORY, "accepted_commit": "a" * 40,
                "accepted_tree": "b" * 40, "pr_number": 7, "pr_head_sha": "d" * 40,
                "source_base_sha": "c" * 40, "pr_run_id": 123, "pr_run_attempt": 2,
                "image": image, "candidate": None,
                "plan": {"artifact_id": 55, "artifact_digest": "sha256:" + "2" * 64,
                         "receipt": {"workflow_path": candidate.PR_WORKFLOW, "run_id": 123,
                                     "run_attempt": 2, "head_sha": "d" * 40, "base_sha": "c" * 40,
                                     "pr_number": 7, "tested_tree": "b" * 40, "selected_images": [],
                                     "content_hashes": {image: content_hash},
                                     "versions": {image: version}}},
                "no_change": {"image": image, "content_hash": content_hash, "version": version},
            }
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps(accepted))
            output = root / "github-output"
            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate, "_skopeo_digest", return_value=registry_digest), \
                    mock.patch.object(candidate, "_registry_identity", return_value={
                        "config_digest": "sha256:" + "3" * 64}) as identity, \
                    mock.patch.object(candidate.subprocess, "run") as run_process:
                candidate.publish_candidate(images.ROOT, image, root, "ghcr.io/iarnvidr", output,
                                            registry_digest)
            identity.assert_called_once()
            run_process.assert_not_called()
            published = json.loads(accepted_path.read_text())
            self.assertEqual(published["published_digest"], registry_digest)
            self.assertEqual(published["published_config_digest"], "sha256:" + "3" * 64)
            self.assertIn(f"digest={registry_digest}\n", output.read_text())

    def test_no_change_plan_cannot_exempt_an_image_selected_by_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = "desktop-base"
            content_hash = images.image_hash(image)
            version = images.version(image)
            accepted = {
                "schema": 1, "repository": candidate.REPOSITORY, "accepted_commit": "a" * 40,
                "accepted_tree": "b" * 40, "pr_number": 7, "pr_head_sha": "d" * 40,
                "source_base_sha": "c" * 40, "pr_run_id": 123, "pr_run_attempt": 2,
                "image": image, "candidate": None,
                "plan": {"artifact_id": 55, "artifact_digest": "sha256:" + "2" * 64,
                         "receipt": {"workflow_path": candidate.PR_WORKFLOW, "run_id": 123,
                                     "run_attempt": 2, "head_sha": "d" * 40, "base_sha": "c" * 40,
                                     "pr_number": 7, "tested_tree": "b" * 40,
                                     "selected_images": [image], "content_hashes": {image: content_hash},
                                     "versions": {image: version}}},
                "no_change": {"image": image, "content_hash": content_hash, "version": version},
            }
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps(accepted))
            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate, "_skopeo_digest") as digest:
                with self.assertRaisesRegex(ValueError, "not bound to the accepted PR plan"):
                    candidate.publish_candidate(images.ROOT, image, root, "ghcr.io/iarnvidr",
                                                root / "github-output", "sha256:" + "1" * 64)
            digest.assert_not_called()

    def test_promotes_same_oci_digest_and_signing_input_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "image.oci.tar"
            manifest, config, index_digest = layout(archive)
            with tarfile.open(archive) as bundle:
                index_raw = bundle.extractfile("index.json").read()
            identity = candidate.oci_identity(archive)
            image = "desktop-base"
            receipt = {
                "image": image, "content_hash": images.image_hash(image),
                "version": images.version(image), "manifest_digest": manifest,
                "config_digest": config, "index_digest": index_digest, **identity,
            }
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps({
                "schema": 1, "repository": candidate.REPOSITORY,
                "accepted_commit": "a" * 40, "accepted_tree": "b" * 40,
                "image": image, "candidate": {"receipt": receipt},
            }))
            output = root / "github-output"

            def run(args, **kwargs):
                if args[1] == "inspect":
                    return __import__("subprocess").CompletedProcess(args, 0, stdout=index_raw)
                return __import__("subprocess").CompletedProcess(args, 0, stdout=b"")

            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate.subprocess, "run", side_effect=run) as run_process:
                candidate.publish_candidate(images.ROOT, image, root, "ghcr.io/iarnvidr", output)

            calls = [call.args[0] for call in run_process.call_args_list]
            self.assertEqual(sum(args[1] == "copy" for args in calls), 2)
            self.assertTrue(all("--preserve-digests" in args for args in calls if args[1] == "copy"))
            self.assertTrue(all("--dest-authfile" in args for args in calls if args[1] == "copy"))
            self.assertIn(f"digest={index_digest}\n", output.read_text())
            published = json.loads(accepted_path.read_text())
            self.assertEqual(published["published_digest"], index_digest)
            self.assertEqual(published["published_tags"], ["h-" + receipt["content_hash"], receipt["version"]])

    def test_existing_canonical_match_still_emits_mapping_without_overwriting_hash_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "image.oci.tar"
            manifest, config, index_digest = layout(archive)
            with tarfile.open(archive) as bundle:
                index_raw = bundle.extractfile("index.json").read()
                manifest_raw = bundle.extractfile("blobs/sha256/" + manifest[7:]).read()
            identity = candidate.oci_identity(archive)
            image = "desktop-base"
            receipt = {"image": image, "content_hash": images.image_hash(image),
                       "version": images.version(image), "manifest_digest": manifest,
                       "config_digest": config, "index_digest": index_digest, **identity}
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps({"schema": 1, "repository": candidate.REPOSITORY,
                "accepted_commit": "a" * 40, "accepted_tree": "b" * 40,
                "image": image, "candidate": {"receipt": receipt}}))
            output = root / "github-output"

            def run(args, **kwargs):
                if args[1] == "inspect" and "--raw" in args:
                    raw = manifest_raw if "@" + manifest in args[-1] else index_raw
                    return __import__("subprocess").CompletedProcess(args, 0, stdout=raw)
                return __import__("subprocess").CompletedProcess(args, 0, stdout=index_raw)

            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate.subprocess, "run", side_effect=run) as run_process:
                candidate.publish_candidate(images.ROOT, image, root, "ghcr.io/iarnvidr", output,
                                            identity["publishable_root_digest"])
            calls = [call.args[0] for call in run_process.call_args_list]
            hash_copies = [args for args in calls if args[1] == "copy" and
                           "h-" + receipt["content_hash"] in args[-1]]
            self.assertEqual(hash_copies, [])
            self.assertFalse(any(args[1] == "copy" for args in calls))
            published = json.loads(accepted_path.read_text())
            self.assertEqual(published["published_digest"], identity["publishable_root_digest"])
            self.assertIn("accepted_commit", published)
            self.assertIn(f"digest={identity['publishable_root_digest']}\n", output.read_text())

    def test_existing_canonical_mismatch_fails_before_registry_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "image.oci.tar"
            manifest, config, index_digest = layout(archive)
            identity = candidate.oci_identity(archive)
            image = "desktop-base"
            receipt = {"image": image, "content_hash": images.image_hash(image),
                       "version": images.version(image), "manifest_digest": manifest,
                       "config_digest": config, "index_digest": index_digest, **identity}
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps({"schema": 1, "repository": candidate.REPOSITORY,
                "accepted_commit": "a" * 40, "accepted_tree": "b" * 40,
                "image": image, "candidate": {"receipt": receipt}}))
            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate, "_skopeo_digest", return_value="sha256:" + "1" * 64), \
                    mock.patch.object(candidate, "_registry_identity", return_value={
                        "config_digest": "sha256:" + "0" * 64}), \
                    mock.patch.object(candidate.subprocess, "run") as run_process:
                with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                    candidate.publish_candidate(images.ROOT, image, root, "ghcr.io/iarnvidr", root / "out",
                                                "sha256:" + "1" * 64)
            run_process.assert_not_called()

    def test_nested_wrapper_publishes_selected_root_digest_and_preserves_attestations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "image.oci.tar"
            manifest, config, wrapper_digest = layout(archive, nested=True)
            with tarfile.open(archive) as bundle:
                outer = json.load(bundle.extractfile("index.json"))
                selected_root = outer["manifests"][0]["digest"]
                raw_index = bundle.extractfile("blobs/sha256/" + selected_root[7:]).read()
            identity = candidate.oci_identity(archive)
            receipt = {"image": "desktop-base", "content_hash": images.image_hash("desktop-base"),
                       "version": images.version("desktop-base"), "manifest_digest": manifest,
                       "config_digest": config, "index_digest": wrapper_digest, **identity}
            accepted_path = root / "accepted-image.json"
            accepted_path.write_text(json.dumps({"schema": 1, "repository": candidate.REPOSITORY,
                "accepted_commit": "a" * 40, "accepted_tree": "b" * 40,
                "image": "desktop-base", "candidate": {"receipt": receipt}}))
            output = root / "github-output"

            def run(args, **kwargs):
                if args[1] == "inspect":
                    return __import__("subprocess").CompletedProcess(args, 0, stdout=raw_index)
                return __import__("subprocess").CompletedProcess(args, 0, stdout=b"")

            with mock.patch.object(candidate, "git", side_effect=["a" * 40, "b" * 40]), \
                    mock.patch.object(candidate.subprocess, "run", side_effect=run) as run_process:
                candidate.publish_candidate(images.ROOT, "desktop-base", root,
                                            "ghcr.io/iarnvidr", output)
            calls = [call.args[0] for call in run_process.call_args_list]
            self.assertEqual(sum(args[1] == "copy" for args in calls), 2)
            self.assertTrue(all("--all" in args and "--preserve-digests" in args
                                for args in calls if args[1] == "copy"))
            published = json.loads(accepted_path.read_text())
            self.assertEqual(published["published_digest"], selected_root)
            self.assertNotEqual(published["published_digest"], wrapper_digest)


class CanonicalProducerReuseTests(unittest.TestCase):
    def test_reuses_verified_canonical_archive_for_actual_smoke_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "canonical.oci.tar"
            output = root / "candidate/image.oci.tar"
            layout(source)
            identity = candidate.oci_identity(source)
            with tarfile.open(source) as archive:
                raw_index = archive.extractfile("index.json").read()

            def run(args, **kwargs):
                if args[1:3] == ["inspect", "--raw"]:
                    return __import__("subprocess").CompletedProcess(args, 0, stdout=raw_index)
                if args[1:3] == ["copy", "--all"]:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(source.read_bytes())
                return __import__("subprocess").CompletedProcess(args, 0, stdout=b"")

            with mock.patch.object(candidate.malrunar_image, "image_hash", return_value="a" * 16), \
                    mock.patch.object(candidate.subprocess, "run", side_effect=run) as run_process:
                self.assertTrue(candidate.reuse_canonical(images.ROOT, "desktop-base", output))
            commands = [call.args[0] for call in run_process.call_args_list]
            verify = next(args for args in commands if args[0] == "cosign")
            self.assertIn("https://github.com/iarnvidr/malrunar/.github/workflows/publish.yml@refs/heads/main",
                          verify)
            copy = next(args for args in commands if args[1] == "copy")
            self.assertIn("--all", copy)
            self.assertIn("--preserve-digests", copy)
            self.assertEqual(candidate.oci_identity(output), identity)

    def test_untrusted_canonical_image_falls_back_to_one_build_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "candidate/image.oci.tar"

            def run(args, **kwargs):
                if args[0] == "cosign":
                    raise __import__("subprocess").CalledProcessError(1, args)
                return __import__("subprocess").CompletedProcess(args, 0, stdout=b'{"manifests":[]}')

            with mock.patch.object(candidate.malrunar_image, "image_hash", return_value="a" * 16), \
                    mock.patch.object(candidate.subprocess, "run", side_effect=run):
                self.assertFalse(candidate.reuse_canonical(images.ROOT, "desktop-base", output))
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
