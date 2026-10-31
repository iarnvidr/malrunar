#!/usr/bin/env python3
"""Load only image inputs verified against an exact PR candidate run.

This is a consumer for E2E/quickstart jobs. It never builds a selected image. The
candidate helper authenticates the plan and producer artifacts; this module
rechecks the image identities before it calls Docker, then verifies the loaded
image IDs before writing the consumer manifest.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_candidate as candidate  # noqa: E402
import malrunar_image as images  # noqa: E402


SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX256 = re.compile(r"^[0-9a-f]{64}$")


def _required_text(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"resolved image is missing {key}")
    return value


def _validate_common(entry: dict, args, targets: set[str]) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("resolved image entry must be an object")
    image = _required_text(entry, "image")
    if image not in targets:
        raise ValueError(f"resolver returned an unrequested image: {image}")
    for key in ("producer_run_id", "producer_run_attempt"):
        if entry.get(key) != (args.run_id if key == "producer_run_id" else args.run_attempt):
            raise ValueError(f"resolved image {image} is from another producer run attempt")
    for key in ("head_sha", "base_sha", "tested_tree"):
        if not SHA1.fullmatch(str(entry.get(key, ""))):
            raise ValueError(f"resolved image {image} has an invalid {key}")
    if entry["tested_tree"] != args.tree:
        raise ValueError(f"resolved image {image} was tested against another source tree")
    content_hash = _required_text(entry, "content_hash")
    expected_hash = images.image_hash(image, root=args.root)
    if content_hash != expected_hash:
        raise ValueError(f"resolved image {image} content hash differs from this checkout")
    version = _required_text(entry, "version")
    if version != images.version(image, root=args.root):
        raise ValueError(f"resolved image {image} version differs from this checkout")
    config_digest = _required_text(entry, "config_digest")
    if not SHA256.fullmatch(config_digest):
        raise ValueError(f"resolved image {image} has an invalid config digest")
    if entry.get("source") not in ("candidate", "canonical"):
        raise ValueError(f"resolved image {image} has an unknown source")
    result = {
        "image": image,
        "source": entry["source"],
        "content_hash": content_hash,
        "version": version,
        "config_digest": config_digest,
        "head_sha": entry["head_sha"],
        "base_sha": entry["base_sha"],
        "tested_tree": entry["tested_tree"],
        "pr_number": entry.get("pr_number"),
    }
    if not isinstance(result["pr_number"], int) or result["pr_number"] < 1:
        raise ValueError(f"resolved image {image} has an invalid PR number")
    return result


def _candidate_action(entry: dict, args, candidate_dir: Path) -> tuple[dict, dict]:
    image = entry["image"]
    artifact_id = str(entry.get("artifact_id", ""))
    artifact_digest = _required_text(entry, "artifact_digest")
    artifact_name = _required_text(entry, "artifact_name")
    job_key = _required_text(entry, "job_key")
    producer_job = _required_text(entry, "producer_job")
    if not artifact_id.isdigit() or not SHA256.fullmatch(artifact_digest):
        raise ValueError(f"candidate {image} has invalid artifact identity")
    expected_name = f"{candidate.IMAGE_PREFIX}-{args.run_id}-{args.run_attempt}-{job_key}"
    if artifact_name != expected_name:
        raise ValueError(f"candidate {image} artifact name does not match its producer job")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", job_key):
        raise ValueError(f"candidate {image} has an invalid job key")
    archive_text = _required_text(entry, "archive_path")
    archive = Path(archive_text)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError(f"candidate {image} archive is missing or is a symlink")
    root = candidate_dir.resolve()
    resolved = archive.resolve(strict=True)
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"candidate {image} archive escaped its private extraction directory")
    identity = candidate.oci_identity(resolved)
    for key in ("archive_sha256", "config_digest"):
        if entry.get(key) != identity.get(key):
            raise ValueError(f"candidate {image} archive {key} differs from its verified receipt")
    root_digest = _required_text(entry, "root_digest")
    if not SHA256.fullmatch(root_digest) or identity.get("publishable_root_digest") != root_digest:
        raise ValueError(f"candidate {image} archive root digest differs from its verified receipt")
    if entry.get("manifest_digest") not in (None, identity.get("manifest_digest")):
        raise ValueError(f"candidate {image} archive manifest digest differs from its verified receipt")
    expected_ref = f"malrunar/{image}:h-{entry['content_hash']}"
    normalized = {
        "manifest_digest": identity["manifest_digest"],
        "archive_sha256": entry["archive_sha256"],
        "root_digest": root_digest,
        "local_reference": expected_ref,
        "artifact_id": int(artifact_id),
        "artifact_digest": artifact_digest,
        "artifact_name": artifact_name,
        "job_key": job_key,
        "producer_job": producer_job,
    }
    action = {"kind": "candidate-copy", "archive": resolved,
              "archive_reference": f"oci-archive:{resolved}:h-{entry['content_hash']}",
              "reference": expected_ref,
              "config_digest": entry["config_digest"]}
    return normalized, action


def _canonical_action(entry: dict) -> tuple[dict, dict]:
    image = entry["image"]
    root_digest = _required_text(entry, "root_digest")
    if not SHA256.fullmatch(root_digest):
        raise ValueError(f"canonical image {image} has an invalid root digest")
    reference = _required_text(entry, "reference")
    expected_reference = f"{candidate.IMAGE_REGISTRY}/{image}@{root_digest}"
    if reference != expected_reference:
        raise ValueError(f"canonical image {image} is not pinned to its verified root digest")
    local_reference = f"malrunar/{image}:h-{entry['content_hash']}"
    return {"root_digest": root_digest, "reference": reference,
            "local_reference": local_reference}, {
        "kind": "pull", "reference": reference, "local_reference": local_reference,
        "config_digest": entry["config_digest"]}


def required_image_closure(targets: set[str]) -> list[str]:
    required = set(targets)
    pending = list(targets)
    while pending:
        image = pending.pop()
        dependencies = list(images.TESTED_WITH.get(image, ()))
        parent = images.IMAGES[image]
        if parent:
            dependencies.append(parent)
        for dependency in dependencies:
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    return [name for name in images.IMAGES if name in required]


def _validate_resolutions(resolved, args, targets, candidate_dir):
    if not isinstance(resolved, list) or not resolved:
        raise ValueError("candidate resolver returned no image inputs")
    names = [entry.get("image") if isinstance(entry, dict) else None for entry in resolved]
    required = set(required_image_closure(targets))
    if len(names) != len(set(names)) or set(names) != required:
        raise ValueError("candidate resolver did not return exactly the requested image dependency closure")
    ordered = [next(entry for entry in resolved if entry["image"] == name)
               for name in images.IMAGES if name in required]
    common = [_validate_common(entry, args, required) for entry in ordered]
    facts = {key: common[0][key] for key in ("pr_number", "head_sha", "base_sha", "tested_tree")}
    if any(any(item[key] != facts[key] for key in facts) for item in common[1:]):
        raise ValueError("resolved images do not share one PR and tested tree")
    actions, entries = [], []
    for entry, record in zip(ordered, common):
        if entry["source"] == "candidate":
            details, action = _candidate_action(entry, args, candidate_dir)
        else:
            details, action = _canonical_action(entry)
        actions.append(action)
        entries.append({**record, **details})
    return facts, entries, actions


def _load_and_verify(actions):
    for action in actions:
        if action["kind"] == "candidate-copy":
            destination = f"docker-daemon:{action['reference']}"
            subprocess.run(["skopeo", "copy", "--override-os", "linux", "--override-arch", "amd64",
                            action["archive_reference"], destination], check=True)
        else:
            subprocess.run(["docker", "pull", "--platform", "linux/amd64", action["reference"]],
                           check=True)
        actual = candidate.image_id(action["reference"])
        if actual != action["config_digest"]:
            raise ValueError(f"loaded image config differs from tested input: {action['reference']}")
        if action["kind"] == "pull":
            subprocess.run(["docker", "tag", action["reference"], action["local_reference"]],
                           check=True)


def _write_manifest(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("refusing to overwrite loaded image manifest")
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def current_run_in_progress(run_id: int, run_attempt: int) -> bool:
    """Allow an in-progress workflow only when it is this exact Actions attempt."""
    return (os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
            and os.environ.get("GITHUB_RUN_ID") == str(run_id)
            and os.environ.get("GITHUB_RUN_ATTEMPT") == str(run_attempt))


def load_tested_images(args):
    targets = {target for item in args.targets for target in item.split()}
    unknown = targets - set(images.IMAGES)
    if not targets or unknown:
        raise ValueError("targets must be a nonempty list of known image names")
    if not SHA1.fullmatch(args.tree):
        raise ValueError("tree must be a full lowercase Git tree SHA")
    if args.run_id < 1 or args.run_attempt < 1:
        raise ValueError("run ID and attempt must be positive integers")
    for path in (args.root, args.pr_event, args.preflight_json):
        if not path.exists() or path.is_symlink():
            raise ValueError(f"required input is missing or symlinked: {path.name}")
    with tempfile.TemporaryDirectory(prefix="malrunar-tested-images-") as tmp:
        candidate_dir = Path(tmp)
        resolved = candidate.resolve_tested_images(
            args.root, [name for name in images.IMAGES if name in targets],
            args.run_id, args.run_attempt, args.pr_event, args.tree, args.preflight_json,
            candidate_dir=candidate_dir,
            current_run_in_progress=current_run_in_progress(args.run_id, args.run_attempt),
        )
        facts, entries, actions = _validate_resolutions(resolved, args, targets, candidate_dir)
        _load_and_verify(actions)
    output = {
        "schema": 1,
        "source_run_id": args.run_id,
        "source_run_attempt": args.run_attempt,
        **facts,
        "images": entries,
    }
    _write_manifest(args.output_manifest, output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--targets", nargs="+", required=True,
                        help="images required by this consumer (space separated or repeated)")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--pr-event", type=Path, required=True)
    parser.add_argument("--tree", required=True, help="checkout tree SHA from git rev-parse HEAD^{tree}")
    parser.add_argument("--preflight-json", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_tested_images(args)
        print(json.dumps({"loaded": len(manifest["images"]),
                          "source_run_id": manifest["source_run_id"],
                          "source_run_attempt": manifest["source_run_attempt"],
                          "manifest": str(args.output_manifest)}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"load_tested_images: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
