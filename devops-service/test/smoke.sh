#!/usr/bin/env bash
set -euo pipefail
ref="${1:?image reference required}"
timeout 60 docker run --rm --network none --entrypoint /opt/malrunar/devops-service/venv/bin/python "$ref" -c '
from malrunar_devops import Backend
from malrunar_devops.board import AzureBoards
from malrunar_devops.wiki import AzureWiki
from malrunar_devops.linked import read_items
import azure.cli.core, azure.identity, requests
try:
    Backend()
except ValueError:
    pass
else:
    raise AssertionError("missing policy must fail closed")
'
set +e
timeout 60 docker run --rm --network none "$ref"
status=$?
set -e
[ "$status" = 78 ] || { echo "unconfigured service returned $status, expected 78" >&2; exit 1; }
timeout 60 docker run --rm --network none --entrypoint /opt/malrunar/node/bin/node "$ref" --input-type=module -e 'await import("@getpaseo/client/internal/daemon-client"); await import("ws");'
