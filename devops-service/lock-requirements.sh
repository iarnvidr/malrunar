#!/usr/bin/env bash
# Resolve public metadata only. No Python download, install, build, account or keyring access.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool="$(command -v uv)"
[ "$("$tool" --version | cut -d' ' -f1-2)" = 'uv 0.13.5' ] || {
    echo 'uv 0.13.5 is required to regenerate this lock' >&2
    exit 1
}
printf '# Python 3.14, Linux amd64; generated with uv 0.13.5. See lock-requirements.sh.\n'
exec env -i PATH=/usr/local/bin:/usr/bin:/bin NETRC=/dev/null \
    UV_PYTHON_DOWNLOADS=never UV_KEYRING_PROVIDER=disabled \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    "$tool" --no-config --no-cache --no-progress pip compile "$root/requirements.txt" \
    --python-version 3.14 --python-platform x86_64-manylinux_2_39 \
    --only-binary :all: --generate-hashes --index-url https://pypi.org/simple \
    --keyring-provider disabled --no-python-downloads --no-header --no-annotate
