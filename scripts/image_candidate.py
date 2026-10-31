#!/usr/bin/env python3
"""Record and promote exact PR-tested OCI image layouts."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import malrunar_image

REPOSITORY = "iarnvidr/malrunar"
IMAGE_REGISTRY = "ghcr.io/iarnvidr"
PR_WORKFLOW = ".github/workflows/pr.yml"
QUICKSTART_WORKFLOW = ".github/workflows/quickstart.yml"
PUBLISH_WORKFLOW = ".github/workflows/publish.yml"
HELPER = "scripts/image_candidate.py"
BUILD = "scripts/build.sh"
SMOKE = "scripts/smoke.sh"
HASHER = "scripts/malrunar_image.py"
CHANGED = "scripts/changed_images.py"
ACTION = ".github/actions/record-image-candidates/action.yml"
LOADER = "scripts/load_tested_images.py"
IMAGE_PREFIX = "malrunar-image-candidates"
PLAN_PREFIX = "malrunar-image-plan"
PREFLIGHT_PREFIX = "malrunar-image-preflight"
QUICKSTART_PREFIX = "malrunar-quickstart-candidate"
MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_CONFIG_BYTES = 256 * 1024


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep GitHub API credentials on GitHub during artifact redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if req.get_method() != "GET" or target.scheme != "https" or target.username or target.password:
            raise ValueError("refusing unsafe GitHub artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and source.netloc != target.netloc:
            for header_set in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(header_set):
                    if name.lower() in {"authorization", "proxy-authorization"}:
                        del header_set[name]
        return redirected


def open_github_request(request, timeout):
    parsed = urllib.parse.urlsplit(request.full_url)
    if (request.get_method() != "GET" or parsed.scheme != "https" or parsed.netloc != "api.github.com"
            or parsed.username or parsed.password):
        raise ValueError("refusing unsafe GitHub request")
    try:
        return urllib.request.build_opener(GitHubRedirectHandler()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        failed = urllib.parse.urlsplit(exc.url or request.full_url)
        raise ValueError(f"GitHub request returned HTTP {exc.code} for {failed.netloc}{failed.path}") from exc


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _docker_inspect_id(reference: str) -> str:
    value = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                           check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"Docker returned an invalid image id for {reference}")
    return value


def image_id(reference: str) -> str:
    """Return the selected linux/amd64 config digest, independent of Docker's image-ID model."""
    _inspect_id, raw, _member = _docker_saved_config(reference)
    return "sha256:" + sha(raw)


def json_env(name: str):
    raw = os.environ.get(name, "[]")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.split()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON array of image names")
    return value


def select(candidates, built):
    built_set = set(built.split())
    unknown = (set(candidates) | built_set) - set(malrunar_image.IMAGES)
    if unknown:
        raise ValueError("unknown image names: " + ", ".join(sorted(unknown)))
    return [image for image in malrunar_image.IMAGES if image in candidates and image in built_set]


def _member_bytes(archive, member, max_size=16 * 1024 * 1024):
    if member.size > max_size:
        raise ValueError("OCI metadata member exceeds the size limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("OCI archive has an unreadable member")
    return stream.read(max_size + 1)


def _blob(archive, members, digest, read=False):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest)):
        raise ValueError(f"OCI descriptor has an invalid SHA-256 digest: {digest!r}")
    member = members.get("blobs/sha256/" + digest.split(":", 1)[1])
    if member is None:
        raise ValueError("OCI blob is missing")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("OCI blob is unreadable")
    hasher, body = hashlib.sha256(), bytearray() if read else None
    while block := stream.read(1024 * 1024):
        hasher.update(block)
        if body is not None:
            if len(body) + len(block) > 16 * 1024 * 1024:
                raise ValueError("OCI attestation metadata exceeds the size limit")
            body.extend(block)
    if hasher.hexdigest() != digest.split(":", 1)[1]:
        raise ValueError("OCI blob digest mismatch")
    return bytes(body) if body is not None else None


def _resolve_oci_descriptor(archive, members, descriptor, inherited_platform, seen, images, attestations):
    annotations = descriptor.get("annotations", {})
    if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
        attestations.append(descriptor)
        return
    digest = descriptor.get("digest")
    if digest in seen:
        raise ValueError("OCI index contains a descriptor cycle")
    raw = _blob(archive, members, digest, read=True)
    body = json.loads(raw)
    platform = descriptor.get("platform") or inherited_platform
    if isinstance(body.get("manifests"), list):
        for child in body["manifests"]:
            _resolve_oci_descriptor(archive, members, child, platform, seen | {digest}, images, attestations)
        return
    if not isinstance(body.get("config"), dict) or not isinstance(body.get("layers"), list):
        raise ValueError("OCI descriptor is neither an image manifest nor an index")
    config_digest = body["config"].get("digest")
    config = json.loads(_blob(archive, members, config_digest, read=True))
    actual_platform = {"os": config.get("os"), "architecture": config.get("architecture")}
    if platform and any(platform.get(key) not in (None, value) for key, value in actual_platform.items()):
        raise ValueError("OCI index platform does not match its image configuration")
    images.append((descriptor, body, actual_platform))


def oci_identity(source):
    source = Path(source)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("OCI archive exceeds the size limit")
    hasher = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    with tarfile.open(source, mode="r:") as archive:
        members, expanded = {}, 0
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("unsafe OCI archive member path")
            name = str(path)
            if member.isdir():
                continue
            if not member.isfile() or name in members:
                raise ValueError("OCI archive contains a non-file or duplicate entry")
            expanded += member.size
            if expanded > MAX_BYTES:
                raise ValueError("OCI archive expands beyond the size limit")
            members[name] = member
        allowed = {"oci-layout", "index.json", "manifest.json", "repositories"}
        if set(members) - allowed - {name for name in members if name.startswith("blobs/sha256/")}:
            raise ValueError("OCI archive contains unexpected files")
        layout_member, index_member = members.get("oci-layout"), members.get("index.json")
        if not layout_member or not index_member:
            raise ValueError("OCI image layout is incomplete")
        layout_raw, index_raw = _member_bytes(archive, layout_member), _member_bytes(archive, index_member)
        if json.loads(layout_raw).get("imageLayoutVersion") != "1.0.0":
            raise ValueError("OCI image layout version is unsupported")
        descriptors = json.loads(index_raw).get("manifests")
        if not isinstance(descriptors, list) or not descriptors:
            raise ValueError("OCI image index has no manifests")
        images, attestations = [], []
        for descriptor in descriptors:
            _resolve_oci_descriptor(archive, members, descriptor, None, set(), images, attestations)
        images = [item for item in images if item[2] == {"os": "linux", "architecture": "amd64"}]
        # Multi-tag OCI exporters can repeat the same descriptor. That is one image
        # identity; distinct manifest digests remain ambiguous and are rejected.
        unique_images = {item[0].get("digest"): item for item in images}
        if len(unique_images) != 1:
            raise ValueError("OCI layout must contain exactly one distinct linux/amd64 image manifest")
        image_descriptor, image_manifest, _ = next(iter(unique_images.values()))
        config_digest = image_manifest.get("config", {}).get("digest")
        for layer in image_manifest.get("layers", []):
            _blob(archive, members, layer.get("digest"))
        predicates, attestation_digests = set(), []
        image_digest = image_descriptor["digest"]
        for descriptor in attestations:
            attestation = json.loads(_blob(archive, members, descriptor.get("digest"), read=True))
            for layer in attestation.get("layers", []):
                predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type", "")
                if not predicate:
                    continue
                statement = json.loads(_blob(archive, members, layer.get("digest"), read=True))
                subjects = statement.get("subject", [])
                if not any(item.get("digest", {}).get("sha256") == image_digest.split(":", 1)[1]
                           for item in subjects):
                    raise ValueError("OCI attestation does not name the image manifest as subject")
                predicates.add(predicate)
                attestation_digests.append(layer["digest"])
        if not any(item.startswith("https://spdx.dev/") for item in predicates):
            raise ValueError("OCI archive has no BuildKit SBOM attestation")
        if not any(item.startswith("https://slsa.dev/provenance/") for item in predicates):
            raise ValueError("OCI archive has no BuildKit provenance attestation")
        return {
            "archive_sha256": hasher.hexdigest(), "index_digest": "sha256:" + sha(index_raw),
            # Skopeo unwraps a singleton OCI index wrapper when it writes the
            # registry object. Bind publication to the descriptor it selects,
            # while retaining index_digest as the byte identity of this archive.
            "publishable_root_digest": _publishable_root_digest(index_raw, archive, members),
            "manifest_digest": image_digest, "config_digest": config_digest,
            "attestation_predicates": sorted(predicates),
            "attestation_digests": sorted(set(attestation_digests)),
        }


def _publishable_root_digest(index_raw, archive, members):
    raw = index_raw
    seen = set()
    while True:
        value = json.loads(raw)
        manifests = value.get("manifests")
        if not isinstance(manifests, list) or len(manifests) != 1:
            return "sha256:" + sha(raw)
        descriptor = manifests[0]
        if descriptor.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest":
            return "sha256:" + sha(raw)
        digest = descriptor.get("digest")
        if digest in seen:
            raise ValueError("OCI index wrapper contains a descriptor cycle")
        child = _blob(archive, members, digest, read=True)
        try:
            child_value = json.loads(child)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "sha256:" + sha(raw)
        if not isinstance(child_value.get("manifests"), list):
            return "sha256:" + sha(raw)
        seen.add(digest)
        raw = child


def _safe_config(value, key=""):
    """Keep diagnostic config useful without copying any credential-looking values."""
    sensitive = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization)", re.I)
    if isinstance(value, dict):
        return {name: _safe_config(child, name) for name, child in value.items()}
    if isinstance(value, list):
        return [_safe_config(child, key) for child in value]
    if isinstance(value, str):
        if key.lower() == "env" and "=" in value and sensitive.search(value.split("=", 1)[0]):
            return value.split("=", 1)[0] + "=***"
        if sensitive.search(key):
            return "***"
        return value[:2048]
    return value


def _flatten_config(value, prefix=""):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            result.update(_flatten_config(child, f"{prefix}.{key}" if prefix else key))
        return result
    if isinstance(value, list):
        return {prefix: value}
    return {prefix: value}


def _docker_saved_config(reference):
    """Read only the selected platform's config from `docker image save`; never save layers."""
    inspect_id = _docker_inspect_id(reference)
    process = subprocess.Popen(["docker", "image", "save", "--platform", "linux/amd64", reference],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    raw, member_name, observed = None, None, []
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if member.name.endswith(".json") or member.name.startswith("blobs/sha256/"):
                    observed.append(member.name[:180])
                if not (member.name.endswith(".json") or member.name.startswith("blobs/sha256/")):
                    continue
                if member.size > MAX_CONFIG_BYTES:
                    continue
                stream = archive.extractfile(member)
                candidate = stream.read(MAX_CONFIG_BYTES + 1) if stream else None
                if not candidate or len(candidate) > MAX_CONFIG_BYTES:
                    continue
                try:
                    document = json.loads(candidate)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(document, dict) or not isinstance(document.get("rootfs"), dict):
                    continue
                if document.get("os") != "linux" or document.get("architecture") != "amd64":
                    continue
                raw, member_name = candidate, member.name
                break
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()
    if raw is None:
        raise ValueError("docker save did not contain the inspected image config; members="
                         + json.dumps(observed[:20], sort_keys=True))
    return inspect_id, raw, member_name


def diagnose_image_identity(image, candidate_dir):
    """Return bounded OCI-versus-Docker config evidence for a failed candidate comparison."""
    archive_path = Path(candidate_dir) / f"{image}.oci.tar"
    identity = oci_identity(archive_path)
    with tarfile.open(archive_path, mode="r:") as archive:
        members = {item.name: item for item in archive.getmembers() if item.isfile()}
        config_member = members.get("blobs/sha256/" + identity["config_digest"].split(":", 1)[1])
        if not config_member or config_member.size > MAX_CONFIG_BYTES:
            raise ValueError("selected OCI config is missing or exceeds the diagnostic size limit")
        config_stream = archive.extractfile(config_member)
        oci_raw = config_stream.read(MAX_CONFIG_BYTES + 1) if config_stream else None
        index_member = members.get("index.json")
        index = json.loads(archive.extractfile(index_member).read()) if index_member else {}
    if not oci_raw or len(oci_raw) > MAX_CONFIG_BYTES:
        raise ValueError("selected OCI config could not be read within the diagnostic size limit")
    oci_config = json.loads(oci_raw)
    reference = f"malrunar/{image}:h-{malrunar_image.image_hash(image)}"
    docker_inspect_id, docker_raw, docker_member = _docker_saved_config(reference)
    docker_config = json.loads(docker_raw)
    safe_oci, safe_docker = _safe_config(oci_config), _safe_config(docker_config)
    left, right = _flatten_config(safe_oci), _flatten_config(safe_docker)
    differences = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            differences.append({"field": key, "oci": left.get(key), "docker": right.get(key)})
            if len(differences) == 100:
                break
    return {
        "image": image,
        "archive_sha256": identity["archive_sha256"],
        "oci_index_digest": identity["index_digest"],
        "oci_manifest_digest": identity["manifest_digest"],
        "oci_config_digest": identity["config_digest"],
        "docker_image_id": docker_inspect_id,
        "docker_config_member": docker_member,
        "docker_saved_config_sha256": "sha256:" + sha(docker_raw),
        "index_descriptors": [
            {"digest": item.get("digest"), "annotations": item.get("annotations", {}),
             "platform": item.get("platform")}
            for item in index.get("manifests", []) if isinstance(item, dict)
        ],
        "oci_config": safe_oci,
        "docker_saved_config": safe_docker,
        "field_differences": differences,
        "differences_truncated": len(differences) == 100,
    }


def diagnose_images(images, candidate_dir, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"schema": 1, "images": [], "errors": []}
    for image in images:
        try:
            evidence["images"].append(diagnose_image_identity(image, candidate_dir))
        except Exception as error:
            evidence["errors"].append({"image": image, "error": str(error)[:500]})
    output.write_text(json.dumps(evidence, sort_keys=True) + "\n")
    print(json.dumps({"diagnostics": str(output), "images": [
        {"image": item["image"], "oci_config_digest": item["oci_config_digest"],
         "docker_image_id": item["docker_image_id"],
         "docker_saved_config_sha256": item["docker_saved_config_sha256"],
         "field_difference_count": len(item["field_differences"])}
        for item in evidence["images"]], "errors": evidence["errors"]}, sort_keys=True))


def record_images(root: Path, output: Path):
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ValueError("image candidate artifacts are created only by pull_request runs")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = payload.get("pull_request") or {}
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or not pr:
        raise ValueError("image candidate event is not a repository pull request")
    job_key = os.environ.get("MALRUNAR_CANDIDATE_JOB", "")
    if not re.fullmatch(r"(?:build-targets--[a-z0-9-]+(?:--[a-z0-9-]+)*|media-bridge|work-browser|session-kit|thunderbird|thunderbird-mcp|msteams-mcp|teams-service)", job_key):
        raise ValueError("candidate job key is not an approved image-test job")
    images = os.environ.get("MALRUNAR_CANDIDATE_IMAGES", "").split()
    if not images or len(set(images)) != len(images):
        raise ValueError("candidate job has no unique image targets")
    output.mkdir(parents=True, exist_ok=True)
    receipts = []
    for image in images:
        if image not in malrunar_image.IMAGES:
            raise ValueError(f"unknown candidate image: {image}")
        hash_value = malrunar_image.image_hash(image, root=root)
        reference = f"malrunar/{image}:h-{hash_value}"
        archive_path = Path(os.environ["MALRUNAR_CANDIDATE_DIR"]) / f"{image}.oci.tar"
        identity = oci_identity(archive_path)
        tested_id = image_id(reference)
        if identity["config_digest"] != tested_id:
            raise ValueError(
                f"candidate OCI image config {identity['config_digest']} differs from smoke-tested "
                f"Docker image ID {tested_id}: {image}")
        parent_name = malrunar_image.IMAGES[image]
        parent = None
        if parent_name:
            parent_hash = malrunar_image.image_hash(parent_name, root=root)
            parent = {
                "name": parent_name, "content_hash": parent_hash,
                "config_digest": image_id(f"malrunar/{parent_name}:h-{parent_hash}"),
            }
        tested_with = []
        for dependency in malrunar_image.TESTED_WITH.get(image, ()):
            dependency_hash = malrunar_image.image_hash(dependency, root=root)
            tested_with.append({
                "name": dependency, "content_hash": dependency_hash,
                "config_digest": image_id(f"malrunar/{dependency}:h-{dependency_hash}"),
            })
        receipt = {
            "schema": 1, "repository": REPOSITORY, "workflow_path": PR_WORKFLOW,
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "event": "pull_request", "job_key": job_key,
            "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
            "head_sha": pr.get("head", {}).get("sha"),
            "base_sha": pr.get("base", {}).get("sha"),
            "pr_number": pr.get("number"),
            "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
            "image": image, "version": malrunar_image.version(image, root=root),
            "content_hash": hash_value, "config_digest": tested_id,
            "parent": parent, "tested_with": tested_with,
            **identity,
        }
        receipts.append(receipt)
        output_image = output / "images" / image
        output_image.mkdir(parents=True, exist_ok=True)
        (output_image / "candidate.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        shutil.copyfile(archive_path, output_image / "image.oci.tar")
    index = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": PR_WORKFLOW,
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": "pull_request", "job_key": job_key,
        "images": [record["image"] for record in receipts],
    }
    (output / "candidate-index.json").write_text(json.dumps(index, sort_keys=True) + "\n")


def record_plan(root: Path, output: Path):
    """Record the trusted PR plan so unchanged images can be bound to that decision."""
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ValueError("image plans are recorded only by pull_request runs")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = payload.get("pull_request") or {}
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or not pr:
        raise ValueError("image plan event is not a repository pull request")
    try:
        selected = json.loads(os.environ.get("MALRUNAR_PLAN_IMAGES", "[]"))
    except json.JSONDecodeError as error:
        raise ValueError("image plan selection is malformed") from error
    if not isinstance(selected, list) or any(not isinstance(image, str) for image in selected):
        raise ValueError("image plan selection is malformed")
    if len(selected) != len(set(selected)) or any(image not in malrunar_image.IMAGES for image in selected):
        raise ValueError("image plan selection is malformed")
    images = [image for image in malrunar_image.IMAGES if image in set(selected)]
    hashes = {image: malrunar_image.image_hash(image, root=root) for image in malrunar_image.IMAGES}
    versions = {image: malrunar_image.version(image, root=root) for image in malrunar_image.IMAGES}
    receipt = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": PR_WORKFLOW,
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": "pull_request", "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "head_sha": pr.get("head", {}).get("sha"),
        "base_sha": pr.get("base", {}).get("sha"),
        "pr_number": pr.get("number"),
        "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
        "selected_images": images, "content_hashes": hashes, "versions": versions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, sort_keys=True) + "\n")


def record_quickstart(root: Path, output: Path):
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("quickstart evidence is recorded only by repository pull_request runs")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = payload.get("pull_request") or {}
    if not pr:
        raise ValueError("quickstart event has no pull request")
    source_path = os.environ.get("MALRUNAR_TESTED_IMAGE_MANIFEST")
    if not source_path:
        raise ValueError("quickstart must record the exact tested-image manifest")
    source_file = Path(source_path)
    if source_file.is_symlink() or not source_file.is_file():
        raise ValueError("quickstart tested-image manifest is missing or unsafe")
    source = json.loads(source_file.read_text())
    tested_tree = git(root, "rev-parse", "HEAD^{tree}")
    expected = {
        "schema": 1,
        "source_run_id": int(os.environ["MALRUNAR_PR_RUN_ID"]),
        "source_run_attempt": int(os.environ["MALRUNAR_PR_RUN_ATTEMPT"]),
        "pr_number": pr.get("number"), "head_sha": pr.get("head", {}).get("sha"),
        "base_sha": pr.get("base", {}).get("sha"), "tested_tree": tested_tree,
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise ValueError("loaded image manifest is not bound to this exact PR producer run")
    if (not isinstance(source.get("images"), list) or not source["images"]
            or any(not isinstance(item, dict) for item in source["images"])):
        raise ValueError("loaded image manifest has no verified image mapping")
    expected_targets = set()
    pending = ["obs-control", "media-bridge"]
    while pending:
        image = pending.pop()
        if image in expected_targets:
            continue
        expected_targets.add(image)
        pending.extend(malrunar_image.TESTED_WITH.get(image, ()))
        parent = malrunar_image.IMAGES[image]
        if parent:
            pending.append(parent)
    if [item.get("image") for item in source["images"]] != [
            image for image in malrunar_image.IMAGES if image in expected_targets]:
        raise ValueError("loaded image manifest does not cover the quickstart dependency closure")
    for item in source["images"]:
        image = item["image"]
        expected_local = f"malrunar/{image}:h-{malrunar_image.image_hash(image, root=root)}"
        if item.get("local_reference") != expected_local:
            raise ValueError(f"loaded image has an invalid local smoke-test reference: {image}")
    receipt = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": QUICKSTART_WORKFLOW,
        "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": "pull_request", "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "head_sha": pr.get("head", {}).get("sha"), "base_sha": pr.get("base", {}).get("sha"),
        "pr_number": pr.get("number"), "tested_tree": tested_tree,
        "check": "quickstart", "source_images": source,
    }
    output.write_text(json.dumps(receipt, sort_keys=True) + "\n")


def api(path: str):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN is required for trusted image promotion")
    request = urllib.request.Request("https://api.github.com/" + path, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with open_github_request(request, timeout=60) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("GitHub response exceeded the bounded image artifact size")
    return json.loads(data)


def zip_members(data: bytes, expected):
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        infos = bundle.infolist()
        names = [item.filename for item in infos if not item.is_dir()]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError("candidate artifact has missing, duplicate, or unexpected files")
        if sum(item.file_size for item in infos) > MAX_BYTES:
            raise ValueError("candidate artifact expands beyond the size limit")
        for item in infos:
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                raise ValueError("candidate artifact contains an unsafe path")
        return {name: bundle.read(name) for name in names}


def download_artifact(metadata, destination: Path):
    if metadata.get("expired") or not str(metadata.get("id", "")).isdigit():
        raise ValueError("image candidate artifact is missing, expired, or invalid")
    digest = metadata.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("image candidate artifact has no immutable digest")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN is required for trusted image promotion")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{metadata['id']}/zip",
        headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                 "X-GitHub-Api-Version": "2022-11-28"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    hasher, total = hashlib.sha256(), 0
    with open_github_request(request, timeout=120) as response, destination.open("wb") as stream:
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > MAX_BYTES:
                raise ValueError("candidate artifact exceeded the bounded download size")
            hasher.update(block)
            stream.write(block)
    if "sha256:" + hasher.hexdigest() != digest:
        raise ValueError("image candidate artifact digest mismatch")
    return destination


def trusted_blobs(paths, accepted_policy_sha, producer_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", producer_sha):
        raise ValueError("candidate receipt has no valid producer workflow SHA")
    for path in paths:
        encoded = urllib.parse.quote(path, safe="/")
        accepted = api(f"repos/{REPOSITORY}/contents/{encoded}?ref={accepted_policy_sha}")["sha"]
        produced = api(f"repos/{REPOSITORY}/contents/{encoded}?ref={producer_sha}")["sha"]
        if accepted != produced:
            raise ValueError(f"candidate producer policy differs from accepted main: {path}")


def successful_run(workflow, pr):
    found = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"head_sha": pr["head"]["sha"], "event": "pull_request",
                                        "per_page": 100, "page": page})
        items = api(f"repos/{REPOSITORY}/actions/workflows/{Path(workflow).name}/runs?{query}")["workflow_runs"]
        found.extend(run for run in items
                     if run.get("repository", {}).get("full_name") == REPOSITORY
                     and run.get("head_repository", {}).get("full_name") == REPOSITORY
                     and run.get("path") == workflow and run.get("event") == "pull_request"
                     and run.get("head_sha") == pr["head"]["sha"] and run.get("conclusion") == "success"
                     )
        if len(items) < 100:
            break
    if not found:
        raise ValueError(f"no successful same-repository {workflow} run for this PR head")
    return max(found, key=lambda run: (run["id"], run["run_attempt"]))


def exact_pr(merge_sha):
    found = []
    for candidate in api(f"repos/{REPOSITORY}/commits/{merge_sha}/pulls"):
        if candidate.get("number"):
            pr = api(f"repos/{REPOSITORY}/pulls/{candidate['number']}")
            if (pr.get("merged") and pr.get("merge_commit_sha") == merge_sha
                    and pr.get("base", {}).get("ref") == "main"):
                found.append(pr)
    if len(found) != 1:
        raise ValueError("accepted main commit does not identify exactly one merged PR")
    pr = found[0]
    if (pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY):
        raise ValueError("fork-produced image candidates cannot be published")
    return pr


def verify_run_receipt(receipt, run, workflow, pr, tree):
    # GitHub can omit the pull_requests association after a PR has merged. The
    # retained immutable receipt binds the run to the PR; the merged PR API
    # object supplies its original base/head facts for independent comparison.
    base_sha = pr.get("base", {}).get("sha")
    expected = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": workflow,
        "run_id": run["id"], "run_attempt": run["run_attempt"], "event": "pull_request",
        "head_sha": pr["head"]["sha"], "base_sha": base_sha,
        "pr_number": pr["number"], "tested_tree": tree,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("candidate receipt does not identify this successful PR run and merge tree")
    return base_sha


def verify_quickstart(pr, tree, merge_sha, source_base_sha, accepted_policy_sha,
                      producer_run, plan, root):
    run = successful_run(QUICKSTART_WORKFLOW, pr)
    artifacts = api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    name = f"{QUICKSTART_PREFIX}-{run['id']}-{run['run_attempt']}"
    found = [item for item in artifacts if item.get("name") == name and not item.get("expired")]
    if len(found) != 1:
        raise ValueError("exact successful quickstart run has no unique receipt artifact")
    with tempfile.TemporaryDirectory() as directory:
        path = download_artifact(found[0], Path(directory) / "quickstart.zip")
        files = zip_members(path.read_bytes(), {"quickstart-candidate.json"})
    receipt = json.loads(files["quickstart-candidate.json"])
    base_sha = verify_run_receipt(receipt, run, QUICKSTART_WORKFLOW, pr, tree)
    if base_sha != source_base_sha:
        raise ValueError("quickstart and image candidate runs were tested against different bases")
    if receipt.get("check") != "quickstart":
        raise ValueError("quickstart receipt does not identify the required end-to-end check")
    source = receipt.get("source_images")
    if not isinstance(source, dict) or any(source.get(key) != value for key, value in {
            "schema": 1, "source_run_id": producer_run["id"],
            "source_run_attempt": producer_run["run_attempt"], "pr_number": pr["number"],
            "head_sha": pr["head"]["sha"], "base_sha": source_base_sha,
            "tested_tree": tree,
    }.items()):
        raise ValueError("quickstart image evidence is not bound to the accepted PR image run")
    expected_images = set()
    pending = ["obs-control", "media-bridge"]
    while pending:
        image = pending.pop()
        if image in expected_images:
            continue
        expected_images.add(image)
        pending.extend(malrunar_image.TESTED_WITH.get(image, ()))
        parent = malrunar_image.IMAGES[image]
        if parent:
            pending.append(parent)
    ordered = [item.get("image") for item in source.get("images", []) if isinstance(item, dict)]
    expected_order = [image for image in malrunar_image.IMAGES if image in expected_images]
    if ordered != expected_order:
        raise ValueError("quickstart image evidence has an incomplete dependency closure")
    preflight_meta = preflight_artifact(producer_run)
    if (preflight_meta.get("workflow_run", {}).get("id") != producer_run["id"]
            or preflight_meta.get("workflow_run", {}).get("head_sha") != pr["head"]["sha"]):
        raise ValueError("quickstart preflight artifact belongs to another PR producer run")
    with tempfile.TemporaryDirectory() as directory:
        preflight_zip = download_artifact(preflight_meta, Path(directory) / "preflight.zip")
        preflight_files = zip_members(preflight_zip.read_bytes(), {"canonical-preflight.json"})
    cached, missing = _preflight_entries(json.loads(preflight_files["canonical-preflight.json"]),
                                         producer_run, pr, tree, root)
    selected = set(plan.get("selected_images", []))
    for entry in source["images"]:
        image = entry["image"]
        if (entry.get("content_hash") != plan["content_hashes"].get(image)
                or entry.get("version") != plan["versions"].get(image)
                or entry.get("local_reference") != f"malrunar/{image}:h-{plan['content_hashes'][image]}"):
            raise ValueError(f"quickstart loaded a different image version: {image}")
        if image in selected:
            candidates = candidate_artifacts(producer_run, image)
            if len(candidates) != 1:
                raise ValueError(f"quickstart image has no unique producer artifact: {image}")
            artifact = candidates[0]
            job_key = artifact["name"].removeprefix(
                f"{IMAGE_PREFIX}-{producer_run['id']}-{producer_run['run_attempt']}-")
            expected_job = _producer_job_name(job_key)
            if (entry.get("source") != "candidate"
                    or str(entry.get("artifact_id")) != str(artifact.get("id"))
                    or entry.get("artifact_digest") != artifact.get("digest")
                    or entry.get("artifact_name") != artifact.get("name")
                    or entry.get("job_key") != job_key
                    or entry.get("producer_job") != expected_job):
                raise ValueError(f"quickstart candidate mapping differs from the exact producer artifact: {image}")
        else:
            record = cached.get(image)
            if (image in missing or record is None or entry.get("source") != "canonical"
                    or entry.get("root_digest") != record["root_digest"]
                    or entry.get("config_digest") != record["config_digest"]
                    or entry.get("reference") != f"{IMAGE_REGISTRY}/{image}@{record['root_digest']}"):
                raise ValueError(f"quickstart canonical mapping differs from preflight: {image}")
    trusted_blobs([QUICKSTART_WORKFLOW, HELPER, BUILD, SMOKE, HASHER, CHANGED, ACTION,
                   "scripts/load_tested_images.py", "examples/quickstart/test/build-images.sh",
                   "examples/quickstart/test/run.sh"],
                  accepted_policy_sha, receipt["workflow_sha"])
    return {"run_id": run["id"], "run_attempt": run["run_attempt"],
            "artifact_id": found[0]["id"], "artifact_digest": found[0]["digest"],
            "workflow_sha": receipt["workflow_sha"]}


def candidate_artifacts(run, image):
    artifacts = api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    prefix = f"{IMAGE_PREFIX}-{run['id']}-{run['run_attempt']}-"
    found = []
    for item in artifacts:
        name = item.get("name", "")
        if not name.startswith(prefix) or item.get("expired"):
            continue
        job_key = name.removeprefix(prefix)
        targets = job_key.removeprefix("build-targets--").split("--") if job_key.startswith("build-targets--") else [job_key]
        if image in targets:
            found.append(item)
    return found


def plan_artifact(run):
    artifacts = api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    name = f"{PLAN_PREFIX}-{run['id']}-{run['run_attempt']}"
    found = [item for item in artifacts if item.get("name") == name and not item.get("expired")]
    if len(found) != 1:
        raise ValueError("successful PR run has no unique image plan artifact")
    return found[0]


def preflight_artifact(run):
    artifacts = api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    name = f"{PREFLIGHT_PREFIX}-{run['id']}-{run['run_attempt']}"
    found = [item for item in artifacts if item.get("name") == name and not item.get("expired")]
    if len(found) != 1:
        raise ValueError("producer run has no unique immutable canonical preflight artifact")
    return found[0]


def wait_for_pr_run(root: Path, pr_event_path: Path, timeout_seconds: int,
                    output: Path, preflight_output: Path, poll_seconds: int = 15):
    """Wait boundedly for the exact PR image producer workflow attempt and fetch its preflight."""
    if timeout_seconds < 1 or timeout_seconds > 3600 or poll_seconds < 1 or poll_seconds > 60:
        raise ValueError("producer wait bounds are invalid")
    event = json.loads(pr_event_path.read_text())
    pr = event.get("pull_request") or {}
    if (event.get("repository", {}).get("full_name") != REPOSITORY
            or not isinstance(pr.get("number"), int)
            or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", pr.get("head", {}).get("sha", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", pr.get("base", {}).get("sha", ""))):
        raise ValueError("quickstart requires a same-repository pull request event")
    current_pr = api(f"repos/{REPOSITORY}/pulls/{pr['number']}")
    if (current_pr.get("number") != pr["number"]
            or current_pr.get("head", {}).get("sha") != pr["head"]["sha"]
            or current_pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
            or current_pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY):
        raise ValueError("quickstart PR no longer matches its triggering head")

    deadline = time.monotonic() + timeout_seconds
    selected = None
    while time.monotonic() < deadline:
        query = urllib.parse.urlencode({"head_sha": pr["head"]["sha"], "event": "pull_request",
                                        "per_page": 100, "page": 1})
        runs = api(f"repos/{REPOSITORY}/actions/workflows/{Path(PR_WORKFLOW).name}/runs?{query}")
        found = [run for run in runs.get("workflow_runs", [])
                 if run.get("repository", {}).get("full_name") == REPOSITORY
                 and run.get("head_repository", {}).get("full_name") == REPOSITORY
                 and run.get("path") == PR_WORKFLOW and run.get("event") == "pull_request"
                 and run.get("head_sha") == pr["head"]["sha"]
                 and (not run.get("pull_requests")
                      or any(item.get("number") == pr["number"] for item in run["pull_requests"]))]
        if found:
            selected = max(found, key=lambda item: (item.get("id", 0), item.get("run_attempt", 0)))
            if selected.get("status") == "completed":
                if selected.get("conclusion") != "success":
                    raise ValueError("exact PR image producer workflow completed unsuccessfully")
                break
        time.sleep(poll_seconds)
    if not selected or selected.get("status") != "completed" or selected.get("conclusion") != "success":
        raise ValueError("timed out waiting for the exact successful PR image producer workflow")

    run_id, run_attempt = selected.get("id"), selected.get("run_attempt")
    if not isinstance(run_id, int) or run_id <= 0 or not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("successful PR image producer run identity is invalid")
    attempt = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}")
    if (attempt.get("id") != run_id or attempt.get("run_attempt") != run_attempt
            or attempt.get("path") != PR_WORKFLOW or attempt.get("event") != "pull_request"
            or attempt.get("status") != "completed" or attempt.get("conclusion") != "success"
            or attempt.get("head_sha") != pr["head"]["sha"]
            or attempt.get("repository", {}).get("full_name") != REPOSITORY
            or (attempt.get("pull_requests") and not any(
                item.get("number") == pr["number"] for item in attempt["pull_requests"]))):
        raise ValueError("successful PR run attempt does not match the exact triggering pull request")
    jobs = _attempt_jobs(run_id, run_attempt)
    for name in ("plan", "canonical-preflight"):
        matching = [job for job in jobs if job.get("name") == name]
        if len(matching) != 1 or matching[0].get("conclusion") != "success":
            raise ValueError(f"PR image producer is missing a successful {name} job")
    manifest_metadata = preflight_artifact({"id": run_id, "run_attempt": run_attempt})
    if (manifest_metadata.get("workflow_run", {}).get("id") != run_id
            or manifest_metadata.get("workflow_run", {}).get("head_sha") != pr["head"]["sha"]):
        raise ValueError("canonical preflight artifact does not belong to the successful PR run")
    with tempfile.TemporaryDirectory() as directory:
        archive = download_artifact(manifest_metadata, Path(directory) / "preflight.zip")
        files = zip_members(archive.read_bytes(), {"canonical-preflight.json"})
    manifest = json.loads(files["canonical-preflight.json"])
    if (manifest.get("run_id") != run_id or manifest.get("run_attempt") != run_attempt
            or manifest.get("pr_number") != pr["number"]
            or manifest.get("head_sha") != pr["head"]["sha"]
            or manifest.get("base_sha") != pr["base"]["sha"]
            or manifest.get("workflow_path") != PR_WORKFLOW
            or manifest.get("registry") != IMAGE_REGISTRY):
        raise ValueError("canonical preflight manifest is not bound to the successful PR run")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "run_id": run_id, "run_attempt": run_attempt,
        "pr_number": pr["number"], "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"], "tested_tree": manifest.get("tested_tree"),
    }, sort_keys=True) + "\n")
    preflight_output.parent.mkdir(parents=True, exist_ok=True)
    preflight_output.write_bytes(files["canonical-preflight.json"])
    return {"run_id": run_id, "run_attempt": run_attempt, "preflight": manifest_metadata}


def validate_plan_receipt(receipt, run, pr, merge_tree, root):
    base_sha = verify_run_receipt(receipt, run, PR_WORKFLOW, pr, merge_tree)
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("workflow_sha", "")):
        raise ValueError("image plan has no valid producer workflow SHA")
    selected = receipt.get("selected_images")
    if not isinstance(selected, list) or len(selected) != len(set(selected)):
        raise ValueError("image plan selection is malformed")
    if any(image not in malrunar_image.IMAGES for image in selected):
        raise ValueError("image plan contains an unknown image")
    if selected != [image for image in malrunar_image.IMAGES if image in set(selected)]:
        raise ValueError("image plan selection is not in canonical order")
    hashes = receipt.get("content_hashes")
    versions = receipt.get("versions")
    expected_images = set(malrunar_image.IMAGES)
    if not isinstance(hashes, dict) or set(hashes) != expected_images:
        raise ValueError("image plan content hash inventory is incomplete")
    if not isinstance(versions, dict) or set(versions) != expected_images:
        raise ValueError("image plan version inventory is incomplete")
    for image in malrunar_image.IMAGES:
        if hashes[image] != malrunar_image.image_hash(image, root=root):
            raise ValueError(f"image plan content hash differs from accepted main: {image}")
        if versions[image] != malrunar_image.version(image, root=root):
            raise ValueError(f"image plan version differs from accepted main: {image}")
    return base_sha


def verify_plan_artifact(metadata, archive_zip: Path, run, pr, merge_tree, accepted_policy_sha, root, image,
                        *, verify_source_policy=True):
    if metadata.get("workflow_run", {}).get("id") != run["id"]:
        raise ValueError("image plan artifact belongs to another workflow run")
    expected_name = f"{PLAN_PREFIX}-{run['id']}-{run['run_attempt']}"
    if metadata.get("name") != expected_name:
        raise ValueError("image plan artifact name does not match its producer run")
    files = zip_members(archive_zip.read_bytes(), {"image-plan.json"})
    receipt = json.loads(files["image-plan.json"])
    base_sha = validate_plan_receipt(receipt, run, pr, merge_tree, root)
    if verify_source_policy:
        paths = [PR_WORKFLOW, HELPER, LOADER, BUILD, SMOKE, HASHER, CHANGED, ACTION]
        trusted_blobs(paths, accepted_policy_sha, receipt["workflow_sha"])
    return receipt, base_sha


def verify_candidate_artifact(metadata, archive_zip: Path, run, pr, merge_sha, merge_tree, image, root, image_path,
                              *, verify_source_policy=True):
    if metadata.get("workflow_run", {}).get("id") != run["id"]:
        raise ValueError("image candidate artifact belongs to another workflow run")
    with zipfile.ZipFile(archive_zip) as inner:
        infos = inner.infolist()
        names = [item.filename for item in infos if not item.is_dir()]
        if len(names) != len(set(names)) or "candidate-index.json" not in names:
            raise ValueError("candidate image artifact has duplicate files or no index")
        if sum(item.file_size for item in infos) > MAX_BYTES:
            raise ValueError("candidate image artifact expands beyond the size limit")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("candidate image artifact contains an unsafe path")
        index = json.loads(inner.read("candidate-index.json"))
        job_key = metadata["name"].removeprefix(f"{IMAGE_PREFIX}-{run['id']}-{run['run_attempt']}-")
        if (index.get("schema") != 1 or index.get("repository") != REPOSITORY
                or index.get("workflow_path") != PR_WORKFLOW or index.get("run_id") != run["id"]
                or index.get("run_attempt") != run["run_attempt"] or index.get("event") != "pull_request"
                or index.get("job_key") != job_key):
            raise ValueError("candidate image bundle metadata does not match its producer artifact")
        listed = index.get("images")
        if not isinstance(listed, list) or len(listed) != len(set(listed)):
            raise ValueError("candidate image index is malformed")
        expected_names = {"candidate-index.json"}
        for target in listed:
            if target not in malrunar_image.IMAGES:
                raise ValueError("candidate image index contains an unknown target")
            expected_names |= {f"images/{target}/candidate.json", f"images/{target}/image.oci.tar"}
        if set(names) != expected_names:
            raise ValueError("candidate image artifact contents differ from its index")
        if image not in index.get("images", []):
            return None
        prefix = f"images/{image}/"
        candidate = json.loads(inner.read(prefix + "candidate.json"))
        info = inner.getinfo(prefix + "image.oci.tar")
        if info.file_size > MAX_BYTES:
            raise ValueError("candidate OCI archive exceeds the size limit")
        image_path.parent.mkdir(parents=True, exist_ok=True)
        with inner.open(info) as source, image_path.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
    base_sha = verify_run_receipt(candidate, run, PR_WORKFLOW, pr, merge_tree)
    expected_job = {
        "desktop-base": "build-targets--", "obs-runtime": "build-targets--", "obs-control": "build-targets--",
        "guacd": "build-targets--", "guacamole-tunnel": "build-targets--", "tail-browser-mcp": "build-targets--",
        "office-convert": "build-targets--", "node": "build-targets--", "devops-service": "build-targets--",
        "media-bridge": "media-bridge", "work-browser": "work-browser", "session-kit": "session-kit",
        "thunderbird": "thunderbird", "thunderbird-mcp": "thunderbird-mcp",
        "msteams-mcp": "msteams-mcp", "teams-service": "teams-service",
    }[image]
    if expected_job.startswith("build-"):
        targets = job_key.removeprefix(expected_job).split("--")
        if image not in targets or not all(item in malrunar_image.IMAGES for item in targets):
            raise ValueError("core image candidate did not come from a successful build matrix job")
    elif job_key != expected_job:
        raise ValueError("component image candidate came from the wrong CI job")
    if (candidate.get("image") != image
            or candidate.get("content_hash") != malrunar_image.image_hash(image, root=root)
            or candidate.get("version") != malrunar_image.version(image, root=root)):
        raise ValueError("candidate image content hash or version differs from accepted main")
    identity = oci_identity(image_path)
    for key, value in identity.items():
        if candidate.get(key) != value:
            raise ValueError(f"candidate OCI {key} differs from its receipt")
    if candidate.get("config_digest") != identity["config_digest"]:
        raise ValueError("candidate config identity differs from the tested image")
    parent_name = malrunar_image.IMAGES[image]
    expected_parent = None
    if parent_name:
        parent_hash = malrunar_image.image_hash(parent_name, root=root)
        expected_parent = {"name": parent_name, "content_hash": parent_hash,
                           "config_digest": candidate.get("parent", {}).get("config_digest")}
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_parent["config_digest"] or ""):
            raise ValueError("candidate did not bind its actual parent image digest")
    if candidate.get("parent") != expected_parent:
        raise ValueError("candidate parent hash differs from accepted main")
    test_deps = []
    for dependency in malrunar_image.TESTED_WITH.get(image, ()):
        dep_hash = malrunar_image.image_hash(dependency, root=root)
        found = next((item for item in candidate.get("tested_with", [])
                      if item.get("name") == dependency), None)
        if not found or found.get("content_hash") != dep_hash or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", found.get("config_digest", "")):
            raise ValueError("candidate does not identify the exact tested dependency images")
        test_deps.append(found)
    if candidate.get("tested_with") != test_deps:
        raise ValueError("candidate dependency inventory is incomplete or unexpected")
    if verify_source_policy:
        paths = [PR_WORKFLOW, HELPER, LOADER, BUILD, SMOKE, HASHER, CHANGED, ACTION]
        # This runs only in the protected main publisher after the PR is merged. Verify
        # the producer's workflow and helper blobs against that exact accepted merge,
        # while the receipt and plan remain bound to the original PR base and tested tree.
        trusted_blobs(paths, merge_sha, candidate["workflow_sha"])
    return {
        "receipt": candidate, "archive_path": image_path, "base_sha": base_sha,
        "artifact_id": metadata["id"], "artifact_digest": metadata["digest"],
        "artifact_name": metadata["name"],
    }


def _producer_job_name(job_key):
    if job_key.startswith("build-targets--"):
        targets = job_key.removeprefix("build-targets--").split("--")
        return "build (" + " ".join(targets) + ")"
    return job_key


def _attempt_jobs(run_id, run_attempt):
    jobs = []
    for page in range(1, 11):
        data = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
                   f"?per_page=100&page={page}")
        jobs.extend(data.get("jobs", []))
        if len(data.get("jobs", [])) < 100:
            break
    return jobs


def _preflight_entries(manifest, run, pr, tested_tree, root):
    expected = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": PR_WORKFLOW,
        "run_id": run["id"], "run_attempt": run["run_attempt"], "event": "pull_request",
        "head_sha": pr["head"]["sha"], "base_sha": pr["base"]["sha"],
        "pr_number": pr["number"], "tested_tree": tested_tree, "registry": IMAGE_REGISTRY,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("canonical preflight manifest does not match the producing PR run")
    cached = manifest.get("cached")
    missing = manifest.get("missing")
    if not isinstance(cached, list) or not isinstance(missing, list):
        raise ValueError("canonical preflight image inventory is malformed")
    by_image = {}
    for item in cached:
        image = item.get("image")
        if image not in malrunar_image.IMAGES or image in by_image:
            raise ValueError("canonical preflight contains a duplicate or unknown image")
        content_hash = malrunar_image.image_hash(image, root=root)
        reference = f"{IMAGE_REGISTRY}/{image}:h-{content_hash}"
        if (item.get("content_hash") != content_hash or item.get("reference") != reference
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("root_digest", ""))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("config_digest", ""))):
            raise ValueError(f"canonical preflight identity is invalid for {image}")
        by_image[image] = item
    missing_by_image = {}
    for item in missing:
        image = item.get("image")
        if image not in malrunar_image.IMAGES or image in missing_by_image or image in by_image:
            raise ValueError("canonical preflight missing inventory is malformed")
        content_hash = malrunar_image.image_hash(image, root=root)
        if (item.get("content_hash") != content_hash
                or item.get("reference") != f"{IMAGE_REGISTRY}/{image}:h-{content_hash}"):
            raise ValueError(f"canonical preflight missing reference is invalid for {image}")
        missing_by_image[image] = item
    if set(by_image) | set(missing_by_image) != set(malrunar_image.IMAGES):
        raise ValueError("canonical preflight inventory does not cover every image")
    return by_image, missing_by_image


def resolve_tested_images(root: Path, targets: list[str], run_id: int, run_attempt: int,
                          pr_event_path: Path, tested_tree: str, preflight_json: Path, *,
                          current_run_in_progress: bool, candidate_dir: Path | None = None) -> list[dict]:
    """Resolve exact PR-tested candidate artifacts or preflight-verified immutable canonical roots.

    This is the unprivileged consumer path used by PR integrations and quickstart. It omits only
    the accepted-main source-blob comparison; all PR, attempt, tree, artifact, image, and dependency
    identities are still checked. The protected publisher continues to use that source comparison.
    """
    if (not isinstance(run_id, int) or run_id <= 0 or not isinstance(run_attempt, int)
            or run_attempt <= 0 or not re.fullmatch(r"[0-9a-f]{40}", tested_tree)):
        raise ValueError("candidate producer run identity is malformed")
    if candidate_dir is None:
        raise ValueError("candidate consumer must provide a temporary archive directory")
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)) \
            or any(not isinstance(image, str) or image not in malrunar_image.IMAGES for image in targets):
        raise ValueError("tested image selection is empty, duplicated, or unknown")
    if git(root, "rev-parse", "HEAD^{tree}") != tested_tree:
        raise ValueError("candidate consumer checkout tree differs from the producer receipt")
    event = json.loads(pr_event_path.read_text())
    pr = event.get("pull_request") or {}
    if (event.get("repository", {}).get("full_name") != REPOSITORY
            or not isinstance(pr.get("number"), int)
            or not re.fullmatch(r"[0-9a-f]{40}", pr.get("head", {}).get("sha", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", pr.get("base", {}).get("sha", ""))
            or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY):
        raise ValueError("candidate consumer requires the exact same-repository PR event")
    pr_number = pr["number"]
    attempt = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}")
    if (attempt.get("id") != run_id or attempt.get("run_attempt") != run_attempt
            or attempt.get("event") != "pull_request" or attempt.get("path") != PR_WORKFLOW
            or attempt.get("head_sha") != pr["head"]["sha"]
            or attempt.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("candidate producer is not the exact same-repository PR workflow attempt")
    if current_run_in_progress:
        if attempt.get("status") not in {"in_progress", "completed"} \
                or (attempt.get("status") == "completed" and attempt.get("conclusion") != "success"):
            raise ValueError("current PR producer workflow is not active or successful")
    elif attempt.get("status") != "completed" or attempt.get("conclusion") != "success":
        raise ValueError("separate workflow requires a terminal successful PR producer run")
    if attempt.get("pull_requests"):
        if not any(item.get("number") == pr_number for item in attempt["pull_requests"]):
            raise ValueError("producer workflow run is associated with a different pull request")

    run = {"id": run_id, "run_attempt": run_attempt}
    jobs = _attempt_jobs(run_id, run_attempt)
    for name in ("plan", "canonical-preflight"):
        matching_jobs = [job for job in jobs if job.get("name") == name]
        if len(matching_jobs) != 1 or matching_jobs[0].get("conclusion") != "success":
            raise ValueError(f"PR image producer is missing a successful {name} job")
    plan_metadata = plan_artifact(run)
    preflight_metadata = preflight_artifact(run)
    for metadata in (plan_metadata, preflight_metadata):
        workflow_run = metadata.get("workflow_run", {})
        if (workflow_run.get("id") != run_id
                or workflow_run.get("head_sha") != pr["head"]["sha"]):
            raise ValueError("plan or preflight artifact belongs to a different PR producer run")
    with tempfile.TemporaryDirectory(dir=candidate_dir) as temporary:
        temporary_root = Path(temporary)
        plan_zip = download_artifact(plan_metadata, temporary_root / "image-plan.zip")
        plan, plan_base = verify_plan_artifact(
            plan_metadata, plan_zip, run, pr, tested_tree, None, root, targets[0],
            verify_source_policy=False)
        if plan_base != pr["base"]["sha"]:
            raise ValueError("image plan base differs from the pull request event")
        preflight_zip = download_artifact(preflight_metadata, temporary_root / "image-preflight.zip")
        preflight_files = zip_members(preflight_zip.read_bytes(), {"canonical-preflight.json"})
        provided_preflight = preflight_json.read_bytes()
        if provided_preflight.rstrip(b"\n") != preflight_files["canonical-preflight.json"].rstrip(b"\n"):
            raise ValueError("supplied canonical preflight differs from its immutable producer artifact")
        preflight = json.loads(preflight_files["canonical-preflight.json"])
        cached, missing = _preflight_entries(preflight, run, pr, tested_tree, root)
        artifacts = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
        artifacts_by_id = {}
        for item in artifacts:
            if (item.get("workflow_run", {}).get("id") == run_id
                    and item.get("workflow_run", {}).get("head_sha") == pr["head"]["sha"]
                    and not item.get("expired")):
                artifacts_by_id[item.get("id")] = item

        required = set(targets)
        pending = list(targets)
        while pending:
            image = pending.pop()
            dependencies = list(malrunar_image.TESTED_WITH.get(image, ()))
            parent = malrunar_image.IMAGES[image]
            if parent:
                dependencies.append(parent)
            for dependency in dependencies:
                if dependency not in required:
                    required.add(dependency)
                    pending.append(dependency)

        resolved = {}
        archive_zips = {}
        for image in [name for name in malrunar_image.IMAGES if name in required]:
            selected = image in plan.get("selected_images", [])
            prefix = f"{IMAGE_PREFIX}-{run_id}-{run_attempt}-"
            matching = []
            for metadata in artifacts_by_id.values():
                name = metadata.get("name", "")
                if not name.startswith(prefix):
                    continue
                key = name.removeprefix(prefix)
                listed = key.removeprefix("build-targets--").split("--") if key.startswith("build-targets--") else [key]
                if image in listed:
                    matching.append(metadata)
            if selected:
                if image in missing:
                    # A selected target may be built once in its producer job; the preflight miss
                    # must be visible and is not itself an error.
                    pass
                if len(matching) != 1:
                    raise ValueError(f"selected image has no unique exact candidate artifact: {image}")
                metadata = matching[0]
                job_key = metadata["name"].removeprefix(prefix)
                expected_job = _producer_job_name(job_key)
                producers = [job for job in jobs if job.get("name") == expected_job]
                if len(producers) != 1 or producers[0].get("conclusion") != "success":
                    raise ValueError(f"selected image producer job did not succeed in this attempt: {image}")
                if metadata["id"] not in archive_zips:
                    archive_zips[metadata["id"]] = download_artifact(
                        metadata, temporary_root / f"candidate-{metadata['id']}.zip")
                archive_path = candidate_dir / f"{image}.oci.tar"
                verified = verify_candidate_artifact(
                    metadata, archive_zips[metadata["id"]], run, pr, None,
                    tested_tree, image, root, archive_path, verify_source_policy=False)
                if verified is None or verified["base_sha"] != plan_base:
                    raise ValueError(f"selected candidate artifact differs from the exact run plan: {image}")
                receipt = verified["receipt"]
                if image in cached and (receipt["publishable_root_digest"] != cached[image]["root_digest"]
                                        or receipt["config_digest"] != cached[image]["config_digest"]):
                    raise ValueError(f"selected candidate differs from its verified canonical image: {image}")
                entry = {
                    "image": image, "source": "candidate", "content_hash": receipt["content_hash"],
                    "version": receipt["version"], "config_digest": receipt["config_digest"],
                    "root_digest": receipt["publishable_root_digest"],
                    "manifest_digest": receipt["manifest_digest"],
                    "archive_path": str(archive_path), "archive_sha256": sha_file(archive_path),
                    "artifact_id": metadata["id"], "artifact_digest": metadata["digest"],
                    "artifact_name": metadata["name"], "job_key": job_key,
                    "producer_job": expected_job, "producer_run_id": run_id,
                    "producer_run_attempt": run_attempt, "head_sha": pr["head"]["sha"],
                    "base_sha": plan_base, "tested_tree": tested_tree,
                    "preflight_artifact_id": preflight_metadata["id"],
                    "preflight_artifact_digest": preflight_metadata["digest"],
                    "preflight_artifact_name": preflight_metadata["name"],
                }
            else:
                if matching:
                    raise ValueError(f"unselected image unexpectedly has a candidate artifact: {image}")
                if image in missing or image not in cached:
                    raise ValueError(f"unselected dependency lacks a preflight-verified canonical image: {image}")
                record = cached[image]
                entry = {
                    "image": image, "source": "canonical", "content_hash": record["content_hash"],
                    "version": malrunar_image.version(image, root=root),
                    "config_digest": record["config_digest"], "root_digest": record["root_digest"],
                    "reference": f"{IMAGE_REGISTRY}/{image}@{record['root_digest']}",
                    "artifact_id": None, "artifact_digest": None, "artifact_name": None,
                    "job_key": None, "producer_job": None, "producer_run_id": run_id,
                    "producer_run_attempt": run_attempt, "head_sha": pr["head"]["sha"],
                    "base_sha": plan_base, "tested_tree": tested_tree,
                    "preflight_artifact_id": preflight_metadata["id"],
                    "preflight_artifact_digest": preflight_metadata["digest"],
                    "preflight_artifact_name": preflight_metadata["name"],
                }
            resolved[image] = entry

        for image, entry in resolved.items():
            if entry["source"] != "candidate":
                continue
            # Candidate receipts are retained from the verified bundle for dependency checks below.
            metadata = next(item for item in artifacts_by_id.values() if item.get("id") == entry["artifact_id"])
            archive_zip = archive_zips[metadata["id"]]
            with zipfile.ZipFile(archive_zip) as bundle:
                receipt = json.loads(bundle.read(f"images/{image}/candidate.json"))
            parent = receipt.get("parent")
            if malrunar_image.IMAGES[image]:
                loaded_parent = resolved.get(malrunar_image.IMAGES[image])
                if not loaded_parent or parent.get("config_digest") != loaded_parent["config_digest"]:
                    raise ValueError(f"candidate parent differs from the exact loaded image: {image}")
            expected_tested = []
            for dependency in malrunar_image.TESTED_WITH.get(image, ()):
                loaded_dependency = resolved.get(dependency)
                match = next((item for item in receipt.get("tested_with", [])
                              if item.get("name") == dependency), None)
                if (not loaded_dependency or not match
                        or match.get("config_digest") != loaded_dependency["config_digest"]
                        or match.get("content_hash") != loaded_dependency["content_hash"]):
                    raise ValueError(f"candidate tested dependency differs from the exact loaded image: {image}")
                expected_tested.append(match)
            if receipt.get("tested_with") != expected_tested:
                raise ValueError(f"candidate tested dependency inventory is incomplete: {image}")
        return [resolved[image] for image in malrunar_image.IMAGES if image in resolved]


def retrieve(root: Path, image: str, merge_sha: str, output: Path):
    if image not in malrunar_image.IMAGES or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
        raise ValueError("image or merge commit is invalid")
    if git(root, "rev-parse", "HEAD^{commit}") != merge_sha:
        raise ValueError("publisher checkout does not match the actual main merge commit")
    merge_tree = git(root, "rev-parse", "HEAD^{tree}")
    pr = exact_pr(merge_sha)
    run = successful_run(PR_WORKFLOW, pr)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as directory:
        temp = Path(directory)
        plan_metadata = plan_artifact(run)
        plan_zip = download_artifact(plan_metadata, temp / "image-plan.zip")
        plan, base_sha = verify_plan_artifact(
            plan_metadata, plan_zip, run, pr, merge_tree, merge_sha, root, image)
        artifacts = candidate_artifacts(run, image)
        if image not in plan["selected_images"]:
            if artifacts:
                raise ValueError(f"unselected image unexpectedly has candidate artifacts: {image}")
            quickstart = verify_quickstart(pr, merge_tree, merge_sha, base_sha, merge_sha,
                                           run, plan, root)
            accepted = {
                "schema": 1, "repository": REPOSITORY, "accepted_commit": merge_sha,
                "accepted_tree": merge_tree, "pr_number": pr["number"], "pr_head_sha": pr["head"]["sha"],
                "source_base_sha": base_sha, "pr_run_id": run["id"],
                "pr_run_attempt": run["run_attempt"], "quickstart": quickstart,
                "image": image, "candidate": None,
                "plan": {"receipt": plan, "artifact_id": plan_metadata["id"],
                         "artifact_digest": plan_metadata["digest"]},
                "no_change": {"image": image, "content_hash": plan["content_hashes"][image],
                              "version": plan["versions"][image]},
            }
            (output / "accepted-image.json").write_text(json.dumps(accepted, sort_keys=True) + "\n")
            return None
        if image not in malrunar_image.IMAGES:
            raise ValueError(f"unknown image candidate: {image}")
        candidates = []
        for metadata in artifacts:
            archive_zip = download_artifact(metadata, temp / f"artifact-{metadata['id']}.zip")
            verified = verify_candidate_artifact(metadata, archive_zip, run, pr, merge_sha,
                                                 merge_tree, image, root, temp / "image.oci.tar")
            if verified:
                candidates.append(verified)
        if len(candidates) != 1:
            raise ValueError(f"expected one exact PR-tested image artifact for {image}, found {len(candidates)}")
        selected = candidates[0]
        if selected["base_sha"] != base_sha:
            raise ValueError("image plan and candidate artifacts were tested against different bases")
        quickstart = verify_quickstart(pr, merge_tree, merge_sha, selected["base_sha"], merge_sha,
                                       run, plan, root)
        archive_path = output / "image.oci.tar"
        shutil.copyfile(selected["archive_path"], archive_path)
        accepted = {
            "schema": 1, "repository": REPOSITORY, "accepted_commit": merge_sha,
            "accepted_tree": merge_tree, "pr_number": pr["number"], "pr_head_sha": pr["head"]["sha"],
            "source_base_sha": selected["base_sha"], "pr_run_id": run["id"],
            "pr_run_attempt": run["run_attempt"], "quickstart": quickstart,
            "image": image, "candidate": {k: v for k, v in selected.items() if k != "archive_path"},
            "plan": {"receipt": plan, "artifact_id": plan_metadata["id"],
                     "artifact_digest": plan_metadata["digest"]},
        }
        (output / "accepted-image.json").write_text(json.dumps(accepted, sort_keys=True) + "\n")
        return selected["receipt"]


def _skopeo_digest(reference):
    authfile = str(Path.home() / ".docker" / "config.json")
    result = subprocess.run(["skopeo", "inspect", "--raw", "--authfile", authfile,
                             f"docker://{reference}"],
                            check=True, capture_output=True)
    return "sha256:" + sha(result.stdout)


def publish_candidate(root: Path, image: str, accepted_dir: Path, registry: str, github_output: Path,
                      existing_digest: str = ""):
    accepted_path = accepted_dir / "accepted-image.json"
    archive_path = accepted_dir / "image.oci.tar"
    accepted = json.loads(accepted_path.read_text())
    if (accepted.get("schema") != 1 or accepted.get("repository") != REPOSITORY
            or accepted.get("image") != image or accepted.get("accepted_commit") != git(root, "rev-parse", "HEAD^{commit}")
            or accepted.get("accepted_tree") != git(root, "rev-parse", "HEAD^{tree}")):
        raise ValueError("accepted image mapping does not match the publisher checkout")
    if accepted.get("candidate") is None:
        plan = accepted.get("plan") or {}
        receipt = plan.get("receipt") or {}
        no_change = accepted.get("no_change") or {}
        if (not existing_digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", existing_digest)
                or receipt.get("workflow_path") != PR_WORKFLOW
                or receipt.get("run_id") != accepted.get("pr_run_id")
                or receipt.get("run_attempt") != accepted.get("pr_run_attempt")
                or receipt.get("head_sha") != accepted.get("pr_head_sha")
                or receipt.get("base_sha") != accepted.get("source_base_sha")
                or receipt.get("pr_number") != accepted.get("pr_number")
                or receipt.get("tested_tree") != accepted.get("accepted_tree")
                or image in receipt.get("selected_images", [])
                or no_change.get("image") != image
                or no_change.get("content_hash") != malrunar_image.image_hash(image, root=root)
                or no_change.get("version") != malrunar_image.version(image, root=root)):
            raise ValueError("unchanged image is not bound to the accepted PR plan")
        if (receipt.get("content_hashes", {}).get(image) != no_change.get("content_hash")
                or receipt.get("versions", {}).get(image) != no_change.get("version")):
            raise ValueError("unchanged image identity differs from the accepted PR plan")
        artifact_digest = plan.get("artifact_digest", "")
        if (not str(plan.get("artifact_id", "")).isdigit()
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest)):
            raise ValueError("unchanged image plan artifact identity is invalid")
        target = f"{registry}/{image}"
        hash_tag = "h-" + no_change["content_hash"]
        if _skopeo_digest(f"{target}:{hash_tag}") != existing_digest:
            raise ValueError("existing canonical h- tag changed during publication; refusing overwrite")
        identity = _registry_identity(f"{target}@{existing_digest}", str(Path.home() / ".docker" / "config.json"))
        if _skopeo_digest(f"{target}:{no_change['version']}") != existing_digest:
            raise ValueError("existing canonical image has a mismatched version tag; refusing copy")
        accepted["published_digest"] = existing_digest
        accepted["published_config_digest"] = identity["config_digest"]
        accepted["published_tags"] = [hash_tag, no_change["version"]]
        accepted_path.write_text(json.dumps(accepted, sort_keys=True) + "\n")
        with github_output.open("a") as stream:
            stream.write(f"digest={existing_digest}\n")
        return
    receipt = accepted["candidate"]["receipt"]
    identity = oci_identity(archive_path)
    for key, value in identity.items():
        if receipt.get(key) != value:
            raise ValueError(f"candidate archive {key} no longer matches the accepted receipt")
    if receipt.get("image") != image or receipt.get("content_hash") != malrunar_image.image_hash(image, root=root):
        raise ValueError("candidate content identity differs from this publisher tree")
    parent = receipt.get("parent")
    if parent:
        parent_ref = f"{registry}/{parent['name']}:h-{parent['content_hash']}"
        subprocess.run(["docker", "pull", "--quiet", parent_ref], check=True,
                       stdout=subprocess.DEVNULL)
        if image_id(parent_ref) != parent["config_digest"]:
            raise ValueError("published parent image differs from the parent tested with the candidate")
    target = f"{registry}/{image}"
    hash_tag = "h-" + receipt["content_hash"]
    digest = receipt["publishable_root_digest"]
    authfile = str(Path.home() / ".docker" / "config.json")
    if existing_digest:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", existing_digest):
            raise ValueError("existing canonical tag has an invalid digest")
        if _skopeo_digest(f"{target}:{hash_tag}") != existing_digest:
            raise ValueError("existing canonical h- tag changed during publication; refusing overwrite")
        existing_identity = _registry_identity(f"{target}@{existing_digest}", authfile)
        if existing_digest != digest or existing_identity["config_digest"] != identity["config_digest"]:
            raise ValueError("existing canonical h- tag differs from the accepted candidate; refusing overwrite")
        if _skopeo_digest(f"{target}:{receipt['version']}") != digest:
            raise ValueError("existing canonical image has a mismatched version tag; refusing copy")
    else:
        subprocess.run(["skopeo", "copy", "--dest-authfile", authfile, "--all", "--preserve-digests",
                        f"oci-archive:{archive_path}", f"docker://{target}:{hash_tag}"], check=True)
        if _skopeo_digest(f"{target}:{hash_tag}") != digest:
            raise ValueError("published OCI root digest differs from the tested candidate")
        subprocess.run(["skopeo", "copy", "--src-authfile", authfile, "--dest-authfile", authfile,
                        "--all", "--preserve-digests",
                        f"docker://{target}@{digest}", f"docker://{target}:{receipt['version']}"], check=True)
        if _skopeo_digest(f"{target}:{receipt['version']}") != digest:
            raise ValueError("version tag does not resolve to the exact tested OCI root")
    accepted["published_digest"] = digest
    accepted["published_tags"] = [hash_tag, receipt["version"]]
    accepted_path.write_text(json.dumps(accepted, sort_keys=True) + "\n")
    with github_output.open("a") as stream:
        stream.write(f"digest={digest}\n")


def _registry_identity(reference, authfile):
    result = subprocess.run(["skopeo", "inspect", "--raw", "--authfile", authfile,
                             f"docker://{reference}"], check=True, capture_output=True)
    raw = result.stdout
    value = json.loads(raw)
    if isinstance(value.get("manifests"), list):
        matches = [item for item in value["manifests"]
                   if item.get("platform", {}).get("os") == "linux"
                   and item.get("platform", {}).get("architecture") == "amd64"]
        if len(matches) != 1:
            raise ValueError("published registry index does not select exactly one linux/amd64 image")
        child = subprocess.run(["skopeo", "inspect", "--raw", "--authfile", authfile,
                                f"docker://{reference.split('@', 1)[0]}@{matches[0]['digest']}"],
                               check=True, capture_output=True)
        value = json.loads(child.stdout)
    config_digest = value.get("config", {}).get("digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest or ""):
        raise ValueError("published registry image has no valid config digest")
    return {"config_digest": config_digest}


def _registry_tag_is_missing(error):
    text = " ".join(
        part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part or "")
        for part in (getattr(error, "stdout", None), getattr(error, "stderr", None))
    ).lower()
    return any(marker in text for marker in (
        "manifest unknown", "manifest not found", "name unknown", "404 not found",
        "404 page not found", "statuscode: 404",
    ))


def _reuse_failure(stage, error=None):
    if isinstance(error, subprocess.CalledProcessError):
        print(f"trusted canonical reuse failed at {stage} (exit {error.returncode}); refusing to rebuild")
    else:
        print(f"trusted canonical reuse failed at {stage}; refusing to rebuild")
    raise SystemExit(2)


def preflight_canonical(root: Path, output: Path | None = None):
    """Verify readable signed main tags and linux/amd64 config identities before build jobs fan out."""
    binding = {}
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        payload = json.loads(Path(event_path).read_text())
        pr = payload.get("pull_request") or {}
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
            if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
                    or not pr or not re.fullmatch(r"[0-9a-f]{40}", pr.get("head", {}).get("sha", ""))
                    or not re.fullmatch(r"[0-9a-f]{40}", pr.get("base", {}).get("sha", ""))
                    or not isinstance(pr.get("number"), int)):
                raise ValueError("canonical preflight requires a same-repository pull request")
            binding = {
                "run_id": int(os.environ["GITHUB_RUN_ID"]),
                "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                "event": "pull_request", "head_sha": pr["head"]["sha"],
                "base_sha": pr["base"]["sha"], "pr_number": pr["number"],
                "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
            }
        elif output is not None or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ValueError("only pull_request or manual workflow_dispatch may run canonical preflight")
    elif output is not None:
        raise ValueError("preflight manifest requires the pull request event binding")
    docker_config = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker")) / "config.json"
    auth_args = ["--authfile", str(docker_config)] if docker_config.is_file() else []
    cached, missing = [], []
    for image in malrunar_image.IMAGES:
        content_hash = malrunar_image.image_hash(image, root=root)
        target = f"{IMAGE_REGISTRY}/{image}"
        reference = f"{target}:h-{content_hash}"
        try:
            raw = subprocess.run(["skopeo", "inspect", "--raw", *auth_args,
                                  f"docker://{reference}"], check=True, capture_output=True).stdout
        except subprocess.CalledProcessError as error:
            if _registry_tag_is_missing(error):
                missing.append({"image": image, "content_hash": content_hash,
                                "reference": reference})
                continue
            _reuse_failure("skopeo inspect preflight", error)
        except OSError as error:
            _reuse_failure("skopeo inspect preflight", error)

        root_digest = "sha256:" + sha(raw)
        try:
            subprocess.run(["cosign", "verify", f"{target}@{root_digest}",
                            "--certificate-identity",
                            "https://github.com/iarnvidr/malrunar/.github/workflows/publish.yml@refs/heads/main",
                            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            document = json.loads(raw)
            descriptors = document.get("manifests")
            if descriptors is not None:
                linux_amd64 = [entry for entry in descriptors
                               if entry.get("platform", {}).get("os") == "linux"
                               and entry.get("platform", {}).get("architecture") == "amd64"]
                if len(linux_amd64) != 1:
                    raise ValueError("canonical index must identify one linux/amd64 image")
                child_digest = linux_amd64[0].get("digest", "")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", child_digest):
                    raise ValueError("canonical linux/amd64 manifest digest is invalid")
                child_raw = subprocess.run(
                    ["skopeo", "inspect", "--raw", *auth_args, f"docker://{target}@{child_digest}"],
                    check=True, capture_output=True).stdout
                document = json.loads(child_raw)
            config_digest = document.get("config", {}).get("digest")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest or ""):
                raise ValueError("canonical image has no valid config digest")
        except (OSError, subprocess.CalledProcessError) as error:
            _reuse_failure("canonical cosign or config preflight", error)
        except (ValueError, KeyError, json.JSONDecodeError):
            _reuse_failure("canonical index/config preflight")
        cached.append({"image": image, "content_hash": content_hash, "reference": reference,
                       "root_digest": root_digest, "config_digest": config_digest})
    manifest = {"schema": 1, "repository": REPOSITORY, "workflow_path": PR_WORKFLOW,
                **binding, "registry": IMAGE_REGISTRY, "cached": cached, "missing": missing}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n")
    print(encoded)
    return manifest


def write_manifest_output(path: Path, key: str, value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
        raise ValueError("GitHub output key is invalid")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with path.open("a") as stream:
        stream.write(f"{key}={encoded}\n")


def reuse_canonical(root: Path, image: str, output: Path):
    """Materialize a trusted main image for PR smoke tests when its inputs match."""
    content_hash = malrunar_image.image_hash(image, root=root)
    target = f"{IMAGE_REGISTRY}/{image}"
    tag = f"h-{content_hash}"
    reference = f"{target}:{tag}"
    docker_config = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker")) / "config.json"
    auth_args = ["--authfile", str(docker_config)] if docker_config.is_file() else []
    try:
        raw = subprocess.run(["skopeo", "inspect", "--raw", *auth_args, f"docker://{reference}"],
                             check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as error:
        if _registry_tag_is_missing(error):
            print(f"no canonical tag for {reference}; building once")
            return False
        _reuse_failure("skopeo inspect", error)
    except OSError as error:
        _reuse_failure("skopeo inspect", error)

    digest = "sha256:" + sha(raw)
    try:
        subprocess.run(["cosign", "verify", f"{target}@{digest}",
                        "--certificate-identity",
                        "https://github.com/iarnvidr/malrunar/.github/workflows/publish.yml@refs/heads/main",
                        "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as error:
        _reuse_failure("cosign verify", error)

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        copy_auth_args = ["--src-authfile", str(docker_config)] if docker_config.is_file() else []
        subprocess.run(["skopeo", "copy", *copy_auth_args, "--all", "--preserve-digests",
                        f"docker://{target}@{digest}", f"oci-archive:{output}:{tag}"], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        output.unlink(missing_ok=True)
        _reuse_failure("skopeo copy", error)

    try:
        identity = oci_identity(output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        output.unlink(missing_ok=True)
        _reuse_failure("OCI identity verification")
    if identity["publishable_root_digest"] != digest:
        output.unlink(missing_ok=True)
        print("trusted canonical reuse failed at OCI root digest verification; refusing to rebuild")
        raise SystemExit(2)
    print(f"reusing trusted canonical {reference} ({digest}) for smoke testing")
    return True


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: image_candidate.py select|record|record-plan|diagnose|quickstart|retrieve|publish-candidate|reuse-canonical|preflight-canonical ...")
    command = sys.argv[1]
    if command == "reuse-canonical":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--image", required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        raise SystemExit(0 if reuse_canonical(args.root.resolve(), args.image,
                                              args.output.resolve()) else 1)
    elif command == "preflight-canonical":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--github-output", type=Path)
        args = parser.parse_args(sys.argv[2:])
        manifest = preflight_canonical(args.root.resolve(), args.output.resolve() if args.output else None)
        if args.github_output:
            write_manifest_output(args.github_output, "manifest", manifest)
    elif command == "select":
        parser = argparse.ArgumentParser()
        parser.add_argument("--candidates", required=True)
        parser.add_argument("--built", required=True)
        args = parser.parse_args(sys.argv[2:])
        print(" ".join(select(json.loads(args.candidates), args.built)))
    elif command == "record":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--out", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        record_images(args.root.resolve(), args.out.resolve())
    elif command == "record-plan":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--out", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        record_plan(args.root.resolve(), args.out.resolve())
    elif command == "diagnose":
        parser = argparse.ArgumentParser()
        parser.add_argument("--images", required=True)
        parser.add_argument("--candidate-dir", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        diagnose_images(args.images.split(), args.candidate_dir, args.output)
    elif command == "quickstart":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        record_quickstart(args.root.resolve(), args.output.resolve())
    elif command == "wait-pr-run":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--pr-event", type=Path, required=True)
        parser.add_argument("--timeout-seconds", type=int, default=2100)
        parser.add_argument("--run-output", type=Path, required=True)
        parser.add_argument("--preflight-output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        result = wait_for_pr_run(args.root.resolve(), args.pr_event.resolve(), args.timeout_seconds,
                                 args.run_output.resolve(), args.preflight_output.resolve())
        print(json.dumps(result, sort_keys=True))
    elif command == "retrieve":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--image", required=True)
        parser.add_argument("--merge-sha", required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        retrieve(args.root.resolve(), args.image, args.merge_sha, args.output.resolve())
    elif command == "publish-candidate":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--image", required=True)
        parser.add_argument("--accepted-dir", type=Path, required=True)
        parser.add_argument("--registry", required=True)
        parser.add_argument("--github-output", type=Path, required=True)
        parser.add_argument("--existing-digest", default="")
        args = parser.parse_args(sys.argv[2:])
        publish_candidate(args.root.resolve(), args.image, args.accepted_dir.resolve(),
                          args.registry, args.github_output, args.existing_digest)
    else:
        raise SystemExit("unknown command: " + command)


if __name__ == "__main__":
    import argparse
    main()
