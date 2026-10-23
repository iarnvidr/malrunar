#!/usr/bin/env python3
"""Package the thunderbird-mcp add-on from a checkout, with the inline-image limits raised (build time only).

  build-xpi.py <checkout> <out-dir> <max-image-base64-bytes> <max-total-base64-bytes> <patch-set>

Upstream ships a prebuilt dist/thunderbird-mcp.xpi with 1 MiB / 4 MiB base64 limits compiled in, and no
setting to change them, so the source is edited and the XPI packed again. Every edit is asserted: if
upstream renames or reshapes what is replaced, the build fails instead of producing an unpatched add-on.
The archive is reproducible (sorted entries, fixed timestamps). Nothing here touches the bridge,
mcp-bridge.cjs, which is used as upstream publishes it.
"""
import json
import re
import sys
import zipfile
from pathlib import Path

DATE = (2026, 1, 1, 0, 0, 0)
MIB = 1048576


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        raise SystemExit(f"build-xpi.py: expected exactly one {what}, found {text.count(old)}")
    return text.replace(old, new)


def main(argv):
    checkout, out, per_image, total, patch_set = Path(argv[1]), Path(argv[2]), int(argv[3]), int(argv[4]), int(argv[5])
    if per_image % MIB or total % MIB or per_image <= 0 or total < per_image:
        raise SystemExit("build-xpi.py: the limits must be positive multiples of 1048576, the total not below the per-image limit")
    ext = checkout / "extension"
    api = ext / "mcp_server" / "api.js"
    text = api.read_text()
    text = replace_once(text, "const MAX_INLINE_IMAGE_BASE64_BYTES = 1 * 1024 * 1024;\n",
                        f"const MAX_INLINE_IMAGE_BASE64_BYTES = {per_image};\n", "per-image limit constant")
    text = replace_once(text, "const MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES = 4 * 1024 * 1024;\n",
                        f"const MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES = {total};\n", "total limit constant")
    text = replace_once(text, "max 1 MiB base64 per image and 4 MiB total",
                        "per-image and total base64 size limits apply, and the active values plus any skipped images are reported in the response",
                        "tool description of the limits")
    api.write_text(text)

    package = json.loads((checkout / "package.json").read_text())
    upstream = package["version"]
    version = f"{upstream}.{patch_set * 100000 + per_image // MIB * 100 + total // MIB}"
    manifest_path = ext / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest["version"] != upstream:
        raise SystemExit(f"build-xpi.py: manifest version {manifest['version']} is not package.json's {upstream}")
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (ext / "buildinfo.json").write_text(json.dumps({"version": f"v{upstream}-malrunar", "builtAt": "reproducible"}) + "\n")

    out.mkdir(parents=True, exist_ok=True)
    target = out / "thunderbird-mcp.xpi"
    files = sorted(p for p in ext.rglob("*") if p.is_file() and p.name != ".DS_Store" and ".git" not in p.relative_to(ext).parts[0])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(ext).as_posix(), DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        packed = archive.read("mcp_server/api.js").decode()
        assert f"const MAX_INLINE_IMAGE_BASE64_BYTES = {per_image};" in packed
        assert f"const MAX_INLINE_IMAGES_TOTAL_BASE64_BYTES = {total};" in packed
        assert json.loads(archive.read("manifest.json"))["version"] == version
    print(f"thunderbird-mcp add-on {version}: inline images up to {per_image} base64 bytes each, {total} in total")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
