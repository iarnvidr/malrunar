# msteams-mcp: the Microsoft Teams MCP server (m0nkmaster/msteams-mcp, MIT) plus the session plumbing
# around it, as a component image.
#
# The image is FROM scratch and holds only:
#   /opt/malrunar/msteams-mcp/app/        upstream at the pinned commit, built, with the patches of patches/
#                                         applied and its production npm dependencies (package-lock.json)
#   /opt/malrunar/msteams-mcp/browsers/   Playwright's patched Firefox build, sha256-verified
#   /opt/malrunar/msteams-mcp/bin/        malrunar-msteams-mcp, the one command (serve, login, status, ...)
#   /opt/malrunar/msteams-mcp/lib/, libexec/, share/   the shell helpers, the Node modules and examples
#   /opt/malrunar/msteams-mcp/README.md, LICENSE, SOURCE.txt, THIRD-PARTY.txt, licenses/
# It needs the `node` component, `desktop-base` and the `work-browser` component (README, "Dependencies"),
# which a consumer adds with their own COPY --link --from lines:
#   COPY --link --from=ghcr.io/iarnvidr/msteams-mcp:<tag> / /
# No supervisord program: the MCP server is a stdio program that a client starts per session, and the
# long-lived browser is the work-browser component's.
#
# Everything is pinned (pins.env): upstream by commit (asserted), its npm dependencies by the lockfile
# that commit contains (`npm ci`), Playwright's Firefox by sha256, the build base by digest, apt by a
# snapshot date. The patches fail the build when they no longer apply (patches/apply.sh).
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS build
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG FETCH_APT_SNAPSHOT
ARG BUILD_NODE_VERSION
ARG BUILD_NODE_SHA256
ARG MSTEAMS_MCP_COMMIT
ARG PLAYWRIGHT_FIREFOX_REVISION
ARG PLAYWRIGHT_FIREFOX_SHA256
RUN --mount=type=bind,source=scripts,target=/scripts \
    . /scripts/apt-snapshot.sh; \
    export APT_SNAPSHOT="${FETCH_APT_SNAPSHOT}"; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends git unzip; \
    malrunar_snapshot_disable
ADD https://nodejs.org/dist/v${BUILD_NODE_VERSION}/node-v${BUILD_NODE_VERSION}-linux-x64.tar.gz /tmp/node.tar.gz
RUN echo "${BUILD_NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/build-node; \
    tar -xzf /tmp/node.tar.gz --strip-components=1 -C /opt/build-node; \
    rm /tmp/node.tar.gz
ENV PATH=/opt/build-node/bin:$PATH

# Upstream at the pinned commit (fetched by commit id and asserted), built with its own lockfile.
RUN git init -q /src; \
    cd /src; \
    git remote add origin https://github.com/m0nkmaster/msteams-mcp.git; \
    n=1; until git fetch -q --depth 1 origin "${MSTEAMS_MCP_COMMIT}"; do [ "$n" -lt 5 ] || exit 1; sleep $((n * 10)); n=$((n + 1)); done; \
    git checkout -q --detach FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "${MSTEAMS_MCP_COMMIT}"
WORKDIR /src
RUN npm ci --ignore-scripts --no-audit --no-fund; \
    npm run build; \
    test -r dist/index.js; \
    test -r dist/cli.js

# The patches (patches/README in ../README.md): the build fails when one no longer applies.
COPY patches/ /patches/
RUN /patches/apply.sh /src

# Production dependencies only, from the same lockfile; no install scripts run (Playwright's browser
# download is one: the browser below is fetched and verified by this recipe instead).
RUN rm -rf node_modules; \
    npm ci --omit=dev --ignore-scripts --no-audit --no-fund; \
    node dist/cli.js --help >/dev/null

COPY scripts/list-licenses.cjs /scripts/list-licenses.cjs
# Playwright's Firefox: download, verify, unpack where Playwright looks for it. The revision must be
# the one the locked Playwright wants, so a lockfile change in a later pin forces a pin change here.
ADD https://cdn.playwright.dev/dbazure/download/playwright/builds/firefox/${PLAYWRIGHT_FIREFOX_REVISION}/firefox-ubuntu-24.04.zip /tmp/firefox.zip
RUN revision="$(node -p "require('/src/node_modules/playwright-core/browsers.json').browsers.find(b => b.name === 'firefox').revision")"; \
    test "$revision" = "${PLAYWRIGHT_FIREFOX_REVISION}"; \
    echo "${PLAYWRIGHT_FIREFOX_SHA256}  /tmp/firefox.zip" | sha256sum -c -; \
    version="$(node -p "require('/src/node_modules/playwright-core/browsers.json').browsers.find(b => b.name === 'firefox').browserVersion")"; \
    out=/out/opt/malrunar/msteams-mcp; \
    mkdir -p "$out/browsers/firefox-${revision}" "$out/app" "$out/licenses"; \
    unzip -q /tmp/firefox.zip -d "$out/browsers/firefox-${revision}"; \
    touch "$out/browsers/firefox-${revision}/INSTALLATION_COMPLETE"; \
    test -x "$out/browsers/firefox-${revision}/firefox/firefox"; \
    cp -a /src/package.json /src/package-lock.json /src/dist /src/node_modules "$out/app/"; \
    cp /src/LICENSE "$out/licenses/msteams-mcp-LICENSE"; \
    cp /src/node_modules/playwright/LICENSE "$out/licenses/playwright-LICENSE"; \
    cp /usr/share/common-licenses/MPL-2.0 "$out/licenses/MPL-2.0"; \
    node /scripts/list-licenses.cjs /src/node_modules "$out/THIRD-PARTY.txt"; \
    commit="$(git -C /src rev-parse HEAD)"; \
    printf 'msteams-mcp (upstream version 0.27.2) built from https://github.com/m0nkmaster/msteams-mcp at commit %s (MIT)\nnpm dependencies: package-lock.json of that commit, production set (npm ci --omit=dev)\npatched at build time by the files in msteams-mcp/patches/ of https://github.com/iarnvidr/malrunar (see README.md)\n\nbrowser: Playwright Firefox build %s (Firefox %s, MPL-2.0), firefox-ubuntu-24.04.zip from cdn.playwright.dev, sha256 %s\n  the browser is built and distributed by the Playwright project from the Mozilla sources with its own patches; it is not the Firefox that Mozilla builds and is not branded or supported as such.\n  Mozilla source: https://hg.mozilla.org/mozilla-unified (the Firefox %s release); Playwright patches: https://github.com/microsoft/playwright/tree/main/browser_patches/firefox\n' \
        "$commit" "${revision}" "${version}" "${PLAYWRIGHT_FIREFOX_SHA256}" "${version}" > "$out/SOURCE.txt"

FROM scratch AS component
COPY --link --from=build /out/ /
COPY --link LICENSE README.md /opt/malrunar/msteams-mcp/
COPY --link bin/ /opt/malrunar/msteams-mcp/bin/
COPY --link lib/ /opt/malrunar/msteams-mcp/lib/
COPY --link libexec/ /opt/malrunar/msteams-mcp/libexec/
COPY --link share/ /opt/malrunar/msteams-mcp/share/
