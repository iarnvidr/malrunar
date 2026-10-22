#!/usr/bin/env bash
# Download the pinned OBS Studio .deb and its Debian source package from Launchpad and verify
# every file against the sha256 values in pins.env.
#
#   obs-runtime/fetch-source.sh <output-dir> [pins.env]
#
# Output files (the release assets for the GPL source offer):
#   obs-studio_<OBS_VERSION>_amd64.deb
#   obs-studio_<OBS_VERSION>.dsc
#   obs-studio_<OBS_VERSION>.debian.tar.xz
#   obs-studio_<OBS_UPSTREAM_VERSION>.orig.tar.gz
#   SHA256SUMS   (sha256sum -c format, covers the four files)
# The .dsc's own Checksums-Sha256 for the two tarballs is cross-checked against pins.env. Files are
# written to *.part first and renamed only after they verify, so a failed run leaves no unverified
# file under its final name. Exits non-zero if anything fails to download or verify.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-}"
pins="${2:-$here/pins.env}"
[ -n "$out" ] || { sed -n '2,16p' "$0" >&2; exit 2; }

pin() {
    local value
    value="$(grep -E "^$1=" "$pins" | tail -1 | cut -d= -f2-)"
    [ -n "$value" ] || { echo "fetch-source: $1 is not set in $pins" >&2; exit 1; }
    printf '%s' "$value"
}
version="$(pin OBS_VERSION)"
upstream="$(pin OBS_UPSTREAM_VERSION)"
ppa="$(pin OBS_PPA_URL)"

mkdir -p "$out"
sums="$(mktemp)"
trap 'rm -f "$sums"' EXIT

fetch() { # <url> <file-name> <sha256>
    local url="$1" name="$2" sha="$3"
    echo "fetching $name"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 10 --connect-timeout 30 -o "$out/$name.part" "$url"
    if [ "$(sha256sum "$out/$name.part" | cut -d' ' -f1)" != "$sha" ]; then
        rm -f "$out/$name.part"
        echo "fetch-source: sha256 mismatch for $name (expected $sha)" >&2
        exit 1
    fi
    mv "$out/$name.part" "$out/$name"
    printf '%s  %s\n' "$sha" "$name" >> "$sums"
}

deb="obs-studio_${version}_amd64.deb"
dsc="obs-studio_${version}.dsc"
debian="obs-studio_${version}.debian.tar.xz"
orig="obs-studio_${upstream}.orig.tar.gz"
source_url="$ppa/+sourcefiles/obs-studio/$version"

fetch "$ppa/+files/$deb" "$deb" "$(pin OBS_DEB_SHA256)"
fetch "$source_url/$dsc" "$dsc" "$(pin OBS_DSC_SHA256)"
fetch "$source_url/$debian" "$debian" "$(pin OBS_DEBIAN_TARBALL_SHA256)"
fetch "$source_url/$orig" "$orig" "$(pin OBS_ORIG_TARBALL_SHA256)"

# The source package's own record of its two tarballs must agree with our pins.
grep -qE "^ $(pin OBS_ORIG_TARBALL_SHA256) [0-9]+ $orig\$" "$out/$dsc" \
    || { echo "fetch-source: $dsc does not list the pinned orig tarball sha256" >&2; exit 1; }
grep -qE "^ $(pin OBS_DEBIAN_TARBALL_SHA256) [0-9]+ $debian\$" "$out/$dsc" \
    || { echo "fetch-source: $dsc does not list the pinned debian tarball sha256" >&2; exit 1; }

sort -k2 "$sums" > "$out/SHA256SUMS"
( cd "$out" && sha256sum -c SHA256SUMS )
echo "OBS $version: deb and source package verified in $out"
