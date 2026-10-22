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
if [ "$status" -ne 0 ]; then
    echo 'public hygiene check failed: remove the lines above' >&2
    exit 1
fi
echo 'public hygiene check passed'
