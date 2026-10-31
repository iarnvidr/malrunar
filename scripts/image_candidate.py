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
import urllib.parse
import urllib.request
import zipfile

import malrunar_image

REPOSITORY = "iarnvidr/malrunar"
PR_WORKFLOW = ".github/workflows/pr.yml"
QUICKSTART_WORKFLOW = ".github/workflows/quickstart.yml"
PUBLISH_WORKFLOW = ".github/workflows/publish.yml"
HELPER = "scripts/image_candidate.py"
BUILD = "scripts/build.sh"
SMOKE = "scripts/smoke.sh"
HASHER = "scripts/malrunar_image.py"
CHANGED = "scripts/changed_images.py"
ACTION = ".github/actions/record-image-candidates/action.yml"
IMAGE_PREFIX = "malrunar-image-candidates"
QUICKSTART_PREFIX = "malrunar-quickstart-candidate"
MAX_BYTES = 8 * 1024 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def image_id(reference: str) -> str:
    value = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                           check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"Docker returned an invalid image id for {reference}")
    return value


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
        raise ValueError("OCI descriptor has an invalid SHA-256 digest")
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
        images = [d for d in descriptors if d.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"
                  and d.get("platform", {}).get("os") == "linux"
                  and d.get("platform", {}).get("architecture") == "amd64"]
        if not images:
            images = [d for d in descriptors if d.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"]
        if len(images) != 1:
            raise ValueError("OCI layout must contain exactly one linux/amd64 image manifest")
        image_descriptor = images[0]
        image_raw = _blob(archive, members, image_descriptor.get("digest"), read=True)
        image_manifest = json.loads(image_raw)
        config_digest = image_manifest.get("config", {}).get("digest")
        _blob(archive, members, config_digest)
        for layer in image_manifest.get("layers", []):
            _blob(archive, members, layer.get("digest"))
        predicates, attestation_digests = set(), []
        image_digest = image_descriptor["digest"]
        for descriptor in descriptors:
            if descriptor is image_descriptor or descriptor.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest":
                continue
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
            "manifest_digest": image_digest, "config_digest": config_digest,
            "attestation_predicates": sorted(predicates),
            "attestation_digests": sorted(set(attestation_digests)),
        }


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
            raise ValueError(f"candidate OCI image differs from the image that passed smoke tests: {image}")
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


def record_quickstart(root: Path, output: Path):
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("quickstart evidence is recorded only by repository pull_request runs")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = payload.get("pull_request") or {}
    if not pr:
        raise ValueError("quickstart event has no pull request")
    receipt = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": QUICKSTART_WORKFLOW,
        "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": "pull_request", "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "head_sha": pr.get("head", {}).get("sha"), "base_sha": pr.get("base", {}).get("sha"),
        "pr_number": pr.get("number"), "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
        "check": "quickstart",
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
    with urllib.request.urlopen(request, timeout=60) as response:
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
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as stream:
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
                     and len(run.get("pull_requests", [])) == 1
                     and run["pull_requests"][0].get("number") == pr["number"])
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
    base_sha = run["pull_requests"][0].get("base", {}).get("sha")
    expected = {
        "schema": 1, "repository": REPOSITORY, "workflow_path": workflow,
        "run_id": run["id"], "run_attempt": run["run_attempt"], "event": "pull_request",
        "head_sha": pr["head"]["sha"], "base_sha": base_sha,
        "pr_number": pr["number"], "tested_tree": tree,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("candidate receipt does not identify this successful PR run and merge tree")
    return base_sha


def verify_quickstart(pr, tree, merge_sha, source_base_sha):
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
    trusted_blobs([QUICKSTART_WORKFLOW, HELPER, BUILD, "examples/quickstart/test/build-images.sh",
                   "examples/quickstart/test/run.sh"], source_base_sha, receipt["workflow_sha"])
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


def verify_candidate_artifact(metadata, archive_zip: Path, run, pr, merge_sha, merge_tree, image, root, image_path):
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
    paths = [PR_WORKFLOW, HELPER, BUILD, SMOKE, HASHER, CHANGED, ACTION]
    # Trust the producer only when its policy files match the base revision against
    # which this PR run was tested. Comparing with the merge commit would let a PR
    # alter both the producer and the comparison target.
    trusted_blobs(paths, base_sha, candidate["workflow_sha"])
    return {
        "receipt": candidate, "archive_path": image_path, "base_sha": base_sha,
        "artifact_id": metadata["id"], "artifact_digest": metadata["digest"],
        "artifact_name": metadata["name"],
    }


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
        candidates = []
        for metadata in candidate_artifacts(run, image):
            archive_zip = download_artifact(metadata, temp / f"artifact-{metadata['id']}.zip")
            verified = verify_candidate_artifact(metadata, archive_zip, run, pr, merge_sha,
                                                 merge_tree, image, root, temp / "image.oci.tar")
            if verified:
                candidates.append(verified)
        if len(candidates) != 1:
            raise ValueError(f"expected one exact PR-tested image artifact for {image}, found {len(candidates)}")
        selected = candidates[0]
        quickstart = verify_quickstart(pr, merge_tree, merge_sha, selected["base_sha"])
        archive_path = output / "image.oci.tar"
        shutil.copyfile(selected["archive_path"], archive_path)
        accepted = {
            "schema": 1, "repository": REPOSITORY, "accepted_commit": merge_sha,
            "accepted_tree": merge_tree, "pr_number": pr["number"], "pr_head_sha": pr["head"]["sha"],
            "source_base_sha": selected["base_sha"], "pr_run_id": run["id"],
            "pr_run_attempt": run["run_attempt"], "quickstart": quickstart,
            "image": image, "candidate": {k: v for k, v in selected.items() if k != "archive_path"},
        }
        (output / "accepted-image.json").write_text(json.dumps(accepted, sort_keys=True) + "\n")
        return selected["receipt"]


def _skopeo_digest(reference):
    authfile = str(Path.home() / ".docker" / "config.json")
    result = subprocess.run(["skopeo", "inspect", "--raw", "--authfile", authfile,
                             f"docker://{reference}"],
                            check=True, capture_output=True)
    return "sha256:" + sha(result.stdout)


def publish_candidate(root: Path, image: str, accepted_dir: Path, registry: str, github_output: Path):
    accepted_path = accepted_dir / "accepted-image.json"
    archive_path = accepted_dir / "image.oci.tar"
    accepted = json.loads(accepted_path.read_text())
    if (accepted.get("schema") != 1 or accepted.get("repository") != REPOSITORY
            or accepted.get("image") != image or accepted.get("accepted_commit") != git(root, "rev-parse", "HEAD^{commit}")
            or accepted.get("accepted_tree") != git(root, "rev-parse", "HEAD^{tree}")):
        raise ValueError("accepted image mapping does not match the publisher checkout")
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
    digest = receipt["index_digest"]
    authfile = str(Path.home() / ".docker" / "config.json")
    subprocess.run(["skopeo", "copy", "--dest-authfile", authfile, "--all", "--preserve-digests",
                    f"oci-archive:{archive_path}", f"docker://{target}:{hash_tag}"], check=True)
    if _skopeo_digest(f"{target}:{hash_tag}") != digest:
        raise ValueError("published OCI index digest differs from the tested candidate")
    subprocess.run(["skopeo", "copy", "--src-authfile", authfile, "--dest-authfile", authfile,
                    "--all", "--preserve-digests",
                    f"docker://{target}@{digest}", f"docker://{target}:{receipt['version']}"], check=True)
    if _skopeo_digest(f"{target}:{receipt['version']}") != digest:
        raise ValueError("version tag does not resolve to the exact tested OCI index")
    accepted["published_digest"] = digest
    accepted["published_tags"] = [hash_tag, receipt["version"]]
    accepted_path.write_text(json.dumps(accepted, sort_keys=True) + "\n")
    with github_output.open("a") as stream:
        stream.write(f"digest={digest}\n")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: image_candidate.py select|record|quickstart|retrieve|publish-candidate ...")
    command = sys.argv[1]
    if command == "select":
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
    elif command == "quickstart":
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        record_quickstart(args.root.resolve(), args.output.resolve())
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
        args = parser.parse_args(sys.argv[2:])
        publish_candidate(args.root.resolve(), args.image, args.accepted_dir.resolve(),
                          args.registry, args.github_output)
    else:
        raise SystemExit("unknown command: " + command)


if __name__ == "__main__":
    import argparse
    main()
