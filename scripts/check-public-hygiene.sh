#!/usr/bin/env bash
# Fail if a tracked file contains something that belongs to a private deployment: personal or
# organisation names, internal addresses, private product environment names. This repository is
# public and its images are generic.
#   scripts/check-public-hygiene.sh            # every tracked file
# The patterns are written with a bracketed letter ([e]sben) so this file does not match itself.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

names='g[i]nnungagap|rod[e]kors|r[e]d[ _-]?cross|[e]stho|[e]sben|\b[a]lice\b|t[a]ilscale|[a]uthelia|c[a]ll studio|v[d]o\.[n]inja'
# Private network ranges and the carrier-grade NAT range VPNs use (the 100.64/10 block).
addresses='\b192\.168\.[0-9]+\.[0-9]+|\b10\.[0-9]+\.[0-9]+\.[0-9]+|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+'
status=0
if git grep -nIiE "$names|$addresses" -- . ':!LICENSE'; then status=1; fi
# Private environment variable prefixes (upper case only: lower case call_ names are ordinary code).
if git grep -nIE '\bC[A]LL_[A-Z0-9_]+|\bR[K]_[A-Z0-9_]+' -- . ':!LICENSE'; then status=1; fi

# Tenant and account data. These patterns do not name anyone: they match the SHAPES that real Microsoft
# 365 data has, so a fixture copied from a real chat, calendar or token fails here whoever it belongs to.
# Fixtures use obviously synthetic values: example.invalid addresses, GUIDs built from one or two repeated
# digits, contoso.sharepoint.com (Microsoft's own fictional tenant).
tracked_text() { git grep -noIE "$1" -- . ':!LICENSE' ':!*package-lock.json' ':!*go.sum' "${@:2}" || true; }
# 1. GUIDs: only synthetic ones (at most two distinct hex digits, like 11111111-1111-... or 00000001-0000-...)
#    and the public identifiers listed here: the Microsoft first-party applications the code talks to (Teams
#    web client, Teams desktop client, Microsoft Graph) and one random namespace constant in obs-control.
allowed_guids='5e3ce6c0-2b1f-4285-8d4b-75ee78787346|1fec8e78-bce4-4aaf-ab1b-5451cc387264|00000003-0000-0000-c000-000000000000|6f1b0d2e-5c0a-4d58-9a43-2f0e6a7c9b11'
guids="$(tracked_text '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' \
    | awk -F: -v ok="$allowed_guids" 'BEGIN { split(ok, a, "|"); for (i in a) allowed[tolower(a[i])] = 1 }
        { value = tolower($3); digits = value; gsub(/-/, "", digits)
          delete seen; distinct = 0
          for (i = 1; i <= length(digits); i++) { c = substr(digits, i, 1); if (!(c in seen)) { seen[c] = 1; distinct++ } }
          if (distinct > 2 && !(value in allowed)) print }')"
if [ -n "$guids" ]; then echo "$guids"; echo 'GUIDs that are neither synthetic nor a listed public identifier (use 11111111-1111-1111-1111-111111111111 style values)' >&2; status=1; fi
# 2. Mail addresses: only reserved or example domains. (File names such as logo@2x.png and the Teams
#    identifier suffixes after a thread id are not addresses.)
mails="$(tracked_text '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}' \
    | grep -viE '@([a-z0-9-]+\.)*(example\.(org|com|net|invalid)|[a-z0-9-]+\.invalid|example|invalid|test|localhost)$|@[0-9]x\.(png|jpg|svg)$|@(unq\.gbl\.spaces|thread\.(v2|tacv2?|skype))$|@users\.noreply\.github\.com$|@(thread|tail)\.' || true)"
if [ -n "$mails" ]; then echo "$mails"; echo 'mail addresses on domains that are not example or reserved ones (use user@example.invalid)' >&2; status=1; fi
# 3. Microsoft tenant host names (a tenant name in front of sharepoint.com or onmicrosoft.com) other than the fictional contoso.
tenants="$(tracked_text '[A-Za-z0-9-]+\.(sharepoint|onmicrosoft)\.com' | grep -viE '(^|[^A-Za-z0-9-])contoso(-[a-z0-9]+)?\.(sharepoint|onmicrosoft)\.com' || true)"
if [ -n "$tenants" ]; then echo "$tenants"; echo 'tenant host names other than contoso' >&2; status=1; fi
# 4. Tokens and identifiers copied from a live session: JWTs, 32-hex Teams chat ids, tailnet names.
live="$(tracked_text 'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}|19:[0-9a-fA-F]{24,}|[A-Za-z0-9-]+\.ts\.net\b')"
if [ -n "$live" ]; then echo "$live"; echo 'something that looks copied from a live session (JWT, Teams chat id, tailnet name)' >&2; status=1; fi
if [ "$status" -ne 0 ]; then
    echo 'public hygiene check failed: remove the lines above' >&2
    exit 1
fi
echo 'public hygiene check passed'
