# Tail browser MCP (https://github.com/rtf6x/tail-browser-mcp, MIT) as a component image.
#
# The output stage is FROM scratch and holds only /opt/malrunar/tail-browser-mcp:
#   server.cjs            Tail MCP server (a Node >= 22 bundle; needs `node` and nothing else)
#   client.mjs            small MCP client for that server (see client-entry.mjs)
#   tail.xpi              Firefox extension, registers with the server as browser id TAIL_BROWSER_ID
#   TAIL-LICENSE, THIRD-PARTY-LICENSES.txt, SOURCE.txt   licence notices (the bundles also keep esbuild's inline legal comments)
# Consumers copy it into their own image: COPY --from=<this image> / /
# The source is patched by prepare-tail.py (structured tab metadata, tab reuse for navigation,
# fixed browser id). Everything is pinned: Tail by commit (asserted), node image by digest, apt by
# a snapshot.debian.org timestamp, npm packages by Tail's lockfiles (`npm ci`).
ARG NODE_IMAGE

FROM ${NODE_IMAGE} AS build
ARG DEBIAN_SNAPSHOT
ARG TAIL_VERSION
ARG TAIL_COMMIT
ARG TAIL_BROWSER_ID
# The node image has no CA bundle yet, so the snapshot is read over http; every index is verified
# through its GPG signature (Signed-By) and verification is never disabled. Snapshot Release files
# carry an expiry that has passed, so only the validity window check is relaxed.
RUN set -eu; . /etc/os-release; \
    test -n "$DEBIAN_SNAPSHOT"; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s/\nSuites: %s %s-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s/\nSuites: %s-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
        "$DEBIAN_SNAPSHOT" "$VERSION_CODENAME" "$VERSION_CODENAME" "$DEBIAN_SNAPSHOT" "$VERSION_CODENAME" > /etc/apt/sources.list.d/debian.sources; \
    retry() { n=1; while ! "$@"; do [ "$n" -lt 5 ] || return 1; sleep $((n * 15)); n=$((n + 1)); done; }; \
    aptget() { apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 -o Acquire::http::Timeout=30 "$@"; }; \
    retry aptget update; \
    retry aptget install -y --no-install-recommends git ca-certificates python3 zip; \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "v${TAIL_VERSION}" https://github.com/rtf6x/tail-browser-mcp.git /tail \
    && test "$(git -C /tail rev-parse HEAD)" = "$TAIL_COMMIT"
COPY prepare-tail.py /tmp/prepare-tail.py
COPY client-entry.mjs /tail/mcp-server/client-entry.mjs
COPY licenses.mjs /tmp/licenses.mjs
RUN set -eu; \
    python3 /tmp/prepare-tail.py /tail "$TAIL_BROWSER_ID"; \
    cd /tail/common && npm ci --ignore-scripts --no-audit --no-fund && npm run build; \
    cd /tail/mcp-server && npm ci --ignore-scripts --no-audit --no-fund && npm run build; \
    mkdir -p /out /meta; \
    ./node_modules/.bin/esbuild http-server.ts --bundle --platform=node --format=cjs --target=node24 \
        --metafile=/meta/server.json --outfile=/out/server.cjs; \
    ./node_modules/.bin/esbuild client-entry.mjs --bundle --platform=node --format=esm --target=node24 \
        --metafile=/meta/client.json "--define:__TAIL_DEFAULT_BROWSER_ID__=\"${TAIL_BROWSER_ID}\"" \
        --outfile=/out/client.mjs; \
    cd /tail/firefox-extension && npm ci --ignore-scripts --no-audit --no-fund; \
    test "$(node -p "require('./package.json').scripts.build")" = 'esbuild background.ts --bundle --outfile=dist/background.js && esbuild options.ts --bundle --outfile=dist/options.js'; \
    ./node_modules/.bin/esbuild background.ts --bundle --metafile=/meta/background.json --outfile=dist/background.js; \
    ./node_modules/.bin/esbuild options.ts --bundle --metafile=/meta/options.json --outfile=dist/options.js; \
    zip -qr -X /out/tail.xpi manifest.json dist options.html assets; \
    cp /tail/LICENSE /out/TAIL-LICENSE; \
    node /tmp/licenses.mjs /out/THIRD-PARTY-LICENSES.txt \
        /tail/mcp-server=/meta/server.json /tail/mcp-server=/meta/client.json \
        /tail/firefox-extension=/meta/background.json /tail/firefox-extension=/meta/options.json; \
    printf 'tail-browser-mcp %s\nsource: https://github.com/rtf6x/tail-browser-mcp at commit %s (MIT)\nbrowser id: %s\nmodified by prepare-tail.py in https://github.com/iarnvidr/malrunar (tail-browser-mcp/)\n' \
        "$TAIL_VERSION" "$TAIL_COMMIT" "$TAIL_BROWSER_ID" > /out/SOURCE.txt

FROM scratch AS component
COPY --from=build /out/ /opt/malrunar/tail-browser-mcp/
