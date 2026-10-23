#!/usr/bin/env python3
"""Patch Thunderbird's bundled OAuth2Module for delegated IMAP accounts.

CONSUMER-SIDE ONLY. This changes Mozilla's source file `modules/OAuth2Module.sys.mjs` inside the
`omni.ja` of a Thunderbird installation. The result is a modified Thunderbird: you may not distribute it
under the Thunderbird name without Mozilla's permission (see ../README.md). The malrunar images never
apply this script; run it in your own image build, on your own copy (`customize.py oauth-shared-mailbox`).

The replacement text below is a modification of that Mozilla file and is therefore offered under the
Mozilla Public License 2.0 (../LICENSE-MPL-2.0); the rest of this repository is Apache-2.0.

The server username remains the mailbox sent in XOAUTH2. A per-server
``oauth2.username`` preference identifies the existing login-manager record
that owns the refresh token, allowing shared mailboxes to reuse that account's
single rotating refresh token.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


REPLACEMENTS = (
    (
        '''    return this.initFromHostname(
      server.hostname,
      server.username,
      server.type,
      customDetails
    );''',
        '''    const username = server.username;
    const oauthUsername = server.getStringValue("oauth2.username") || username;
    return this.initFromHostname(
      server.hostname,
      username,
      server.type,
      customDetails,
      oauthUsername
    );''',
    ),
    (
        '  initFromHostname(hostname, username, type, customDetails) {',
        '  _setUsernamesFromAccessToken(username) {\n    const delegated = this._username !== null && this._username !== this._oauthUsername;\n    this._oauth.username = this._oauthUsername = username;\n    if (!delegated) {\n      this._username = username;\n    }\n  },\n\n  initFromHostname(hostname, username, type, customDetails, oauthUsername = username) {',
    ),
    (
        '    this._username = username;\n    // loginOrigin is needed to save the refresh token in the password manager.',
        '    this._username = username;\n    // Keep mailbox identity (SASL) separate from the credential owner.\n    this._oauthUsername = oauthUsername;\n    // loginOrigin is needed to save the refresh token in the password manager.',
    ),
    ('oauth.username == username &&', 'oauth.username == this._oauthUsername &&'),
    ('this._oauth.username = username;', 'this._oauth.username = this._oauthUsername;'),
    ('[["login_hint", username]]', '[["login_hint", this._oauthUsername]]'),
    ('formatStringFromName("oauth2WindowTitle", [username, hostname]);', 'formatStringFromName("oauth2WindowTitle", [this._oauthUsername, hostname]);'),
    ('login.username != this._username', 'login.username != this._oauthUsername'),
    ('login.init(this._loginOrigin, null, scope, this._username, token, "", "");', 'login.init(this._loginOrigin, null, scope, this._oauthUsername, token, "", "");'),
    ('this._oauth.username = this._username =\n                    jwt.preferred_username;', 'this._setUsernamesFromAccessToken(jwt.preferred_username);'),
    ('`${this._loginOrigin}/${this._username}`', '`${this._loginOrigin}/${this._oauthUsername}`'),
)


def patch_source(source: str) -> str:
    for old, new in REPLACEMENTS:
        count = source.count(old)
        expected = 3 if old == 'login.username != this._username' else 1
        if count != expected:
            raise ValueError(f"Thunderbird OAuth2Module context changed: expected {expected} occurrence(s), found {count}")
        source = source.replace(old, new)
    if source.count('`user=${this._username}\\x01auth=Bearer ${retval}\\x01\\x01`') != 1:
        raise ValueError("Thunderbird SASL mailbox username context changed")
    if source.count("this._setUsernamesFromAccessToken(jwt.preferred_username);") != 1:
        raise ValueError("Thunderbird token username mapping is incomplete")
    return source


def patch_module(path: Path) -> None:
    source = path.read_text()
    path.write_text(patch_source(source))


def patch_omni(path: Path, entry: str) -> None:
    temporary = path.with_suffix(path.suffix + ".malrunar-tmp")
    found = False
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        target.comment = source.comment
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == entry:
                payload = patch_source(payload.decode("utf-8")).encode("utf-8")
                found = True
            target.writestr(info, payload)
    if not found:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"Thunderbird omni.ja is missing {entry}")
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            temporary.unlink(missing_ok=True)
            raise ValueError("Patched Thunderbird omni.ja failed its ZIP integrity check")
        patched = archive.read(entry).decode("utf-8")
        if "server.getStringValue(\"oauth2.username\")" not in patched:
            raise ValueError("Patched OAuth2Module is missing the delegated login mapping")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--entry", default="modules/OAuth2Module.sys.mjs")
    parser.add_argument("--omni", action="store_true")
    args = parser.parse_args()
    if args.omni:
        patch_omni(args.path, args.entry)
    else:
        patch_module(args.path)


if __name__ == "__main__":
    main()
