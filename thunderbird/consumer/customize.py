#!/usr/bin/env python3
"""Consumer-side changes to a Thunderbird installation (standard library only).

The thunderbird component ships Mozilla's Thunderbird unmodified and never runs this tool. A consumer who
wants any of the changes below runs it in ITS OWN image build, on its own copy. That makes the result a
modified Thunderbird, which Mozilla's trademark policy does not let you distribute under the Thunderbird
name without permission; see ../README.md.

  customize.py [--app DIR] oauth-shared-mailbox
      Patch modules/OAuth2Module.sys.mjs inside omni.ja so a delegated (shared) IMAP mailbox can sign in
      with the refresh token of the account that owns it (oauth_shared_mailbox.py).
  customize.py [--app DIR] policies FILE...
      Deep-merge JSON policy fragments (see policies/ and the components that ship their own) into
      <app>/distribution/policies.json, creating it.
  customize.py [--app DIR] addon NAME URL SHA256 [--dir DIR] [--max-version V]
      Download an add-on, check its sha256, raise strict_max_version in its manifest (default: the major
      version of the installed Thunderbird, for example 158.*) and write DIR/NAME.xpi
      (DIR defaults to $MALRUNAR_THUNDERBIRD_ADDON_DIR, else /opt/malrunar/thunderbird-addons).
  customize.py [--app DIR] addon-pinned NAME [--dir DIR] [--max-version V]
      The same for an add-on pinned in addons.json next to this file (tbsync, eas4tbsync).
  customize.py [--app DIR] verify
      Check omni.ja is a sound zip and every policy file is valid JSON.

Every change appends a line to <app>/../MODIFIED.txt, so the installation says it is modified. After
changing omni.ja, delete the profile's startupCache directory (Thunderbird rebuilds it).
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oauth_shared_mailbox  # noqa: E402

DEFAULT_APP = "/opt/malrunar/thunderbird/app"
DEFAULT_ADDON_DIR = "/opt/malrunar/thunderbird-addons"


class Failure(Exception):
    pass


def app_dir(args):
    app = Path(args.app or os.environ.get("MALRUNAR_THUNDERBIRD_APP") or DEFAULT_APP)
    if not (app / "thunderbird").is_file() or not (app / "omni.ja").is_file():
        raise Failure(f"{app} is not a Thunderbird installation (no thunderbird and omni.ja). "
                      "Copy the thunderbird component into the image first, or pass --app.")
    return app


def note_modified(app, text):
    with open(app.parent / "MODIFIED.txt", "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def installed_major(app):
    ini = (app / "application.ini").read_text()
    match = re.search(r"^Version=(\d+)\.", ini, re.MULTILINE)
    if not match:
        raise Failure(f"no Version= in {app}/application.ini")
    return match.group(1)


def merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def cmd_oauth(args):
    app = app_dir(args)
    oauth_shared_mailbox.patch_omni(app / "omni.ja", "modules/OAuth2Module.sys.mjs")
    note_modified(app, "omni.ja: modules/OAuth2Module.sys.mjs patched by oauth-shared-mailbox (MPL-2.0)")
    print("oauth-shared-mailbox: omni.ja patched")


def cmd_policies(args):
    app = app_dir(args)
    target = app / "distribution" / "policies.json"
    policies = {"policies": {}}
    if target.exists():
        policies = json.loads(target.read_text())
    for name in args.files:
        fragment = json.loads(Path(name).read_text())
        if not isinstance(fragment.get("policies"), dict):
            raise Failure(f"{name}: expected a top-level \"policies\" object")
        merge(policies, fragment)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(policies, indent=2) + "\n")
    note_modified(app, "distribution/policies.json written from: " + ", ".join(Path(n).name for n in args.files))
    print(f"policies: wrote {target} ({', '.join(sorted(policies['policies']))})")


def rewrite_max_version(data, max_version):
    """Return (new_xpi_bytes, id, old_max) with strict_max_version set to max_version."""
    source = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(source.read("manifest.json"))
    key = "browser_specific_settings" if "browser_specific_settings" in manifest else "applications"
    gecko = (manifest.get(key) or {}).get("gecko")
    if not gecko:
        raise Failure(f"no {key}.gecko block in the add-on manifest: Thunderbird would refuse it with no explanation")
    old = gecko.get("strict_max_version", "<unset>")
    gecko["strict_max_version"] = max_version
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "manifest.json":
                payload = json.dumps(manifest, indent=2).encode("utf-8")
            target.writestr(info, payload)
    return out.getvalue(), gecko.get("id", "?"), old


def install_addon(app, name, url, sha256, directory, max_version):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise Failure(f"add-on name {name!r}: use lower case letters, digits, - and _")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise Failure("the sha256 must be 64 lower case hex digits")
    if not url.startswith("https://"):
        raise Failure("add-on URLs must be https")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise Failure(f"{url}: sha256 is {actual}, pinned {sha256}")
    max_version = max_version or f"{installed_major(app)}.*"
    patched, addon_id, old = rewrite_max_version(data, max_version)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.xpi").write_bytes(patched)
    note_modified(app, f"add-on {name} ({addon_id}): strict_max_version {old} -> {max_version}, written to {directory}/{name}.xpi")
    print(f"addon {name}: {addon_id} strict_max_version {old} -> {max_version}: {directory}/{name}.xpi")


def addon_dir(args):
    return Path(args.dir or os.environ.get("MALRUNAR_THUNDERBIRD_ADDON_DIR") or DEFAULT_ADDON_DIR)


def cmd_addon(args):
    install_addon(app_dir(args), args.name, args.url, args.sha256, addon_dir(args), args.max_version)


def cmd_addon_pinned(args):
    pins = json.loads((HERE / "addons.json").read_text())
    if args.name not in pins:
        raise Failure(f"no pinned add-on {args.name!r}; pinned: {', '.join(sorted(pins))}")
    entry = pins[args.name]
    install_addon(app_dir(args), args.name, entry["url"], entry["sha256"], addon_dir(args), args.max_version)


def cmd_verify(args):
    app = app_dir(args)
    with zipfile.ZipFile(app / "omni.ja") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise Failure(f"omni.ja: corrupt entry {bad}")
    policies = app / "distribution" / "policies.json"
    if policies.exists():
        json.loads(policies.read_text())
    print("verify: omni.ja is sound" + (", policies.json is valid JSON" if policies.exists() else ""))


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", help=f"the Thunderbird directory (default $MALRUNAR_THUNDERBIRD_APP, else {DEFAULT_APP})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("oauth-shared-mailbox").set_defaults(run=cmd_oauth)
    policies = sub.add_parser("policies")
    policies.add_argument("files", nargs="+")
    policies.set_defaults(run=cmd_policies)
    addon = sub.add_parser("addon")
    addon.add_argument("name")
    addon.add_argument("url")
    addon.add_argument("sha256")
    addon.add_argument("--dir")
    addon.add_argument("--max-version")
    addon.set_defaults(run=cmd_addon)
    pinned = sub.add_parser("addon-pinned")
    pinned.add_argument("name")
    pinned.add_argument("--dir")
    pinned.add_argument("--max-version")
    pinned.set_defaults(run=cmd_addon_pinned)
    sub.add_parser("verify").set_defaults(run=cmd_verify)
    args = parser.parse_args(argv)
    try:
        args.run(args)
    except (Failure, ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print(f"customize.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
