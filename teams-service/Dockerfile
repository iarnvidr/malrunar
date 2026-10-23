# teams-service: a local PocketBase copy of Microsoft Teams chats and calendar, behind a small HTTP API,
# as a component image.
#
# The image is FROM scratch and holds only:
#   /opt/malrunar/teams-service/bin/malrunar-teams-service     the supervisord program's script: starts PocketBase and the service
#   /opt/malrunar/teams-service/bin/malrunar-teams-pocketbase  PocketBase (pinned version) with the migrations compiled in
#   /opt/malrunar/teams-service/src/, dev/, package.json       the service (Node, no npm dependencies) and its synthetic fixture server
#   /opt/malrunar/teams-service/lib/common.sh                  shell helpers
#   /opt/malrunar/teams-service/README.md, LICENSE, SOURCE.txt, THIRD-PARTY.txt, licenses/
#   /etc/supervisor/conf.d/malrunar-teams-service.conf         the supervisord program (the supervisor.d convention, CONVENTIONS.md)
# It needs the `node` component and the package of the `msteams-mcp` component (it imports that package's API
# modules and reads the session that component signed in); README, "Dependencies". Consumers copy it into
# their own image:
#   COPY --link --from=ghcr.io/iarnvidr/teams-service:<tag> / /
#
# PocketBase is pinned by version (pins.env, asserted against pocketbase/go.mod) and by the sha256 of every
# Go module in pocketbase/go.sum (`-mod=readonly`); the toolchain image by digest.
ARG GO_IMAGE

FROM ${GO_IMAGE} AS pocketbase
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG POCKETBASE_VERSION
ENV CGO_ENABLED=0 GOFLAGS=-mod=readonly GOTOOLCHAIN=local
WORKDIR /src
COPY pocketbase/go.mod pocketbase/go.sum ./
RUN test "$(go list -m -f '{{.Version}}' github.com/pocketbase/pocketbase)" = "v${POCKETBASE_VERSION}"; \
    grep -q "^github.com/pocketbase/pocketbase v${POCKETBASE_VERSION} h1:" go.sum; \
    n=1; until go mod download; do [ "$n" -lt 5 ] || exit 1; sleep $((n * 10)); n=$((n + 1)); done
COPY pocketbase/ ./
RUN out=/out/opt/malrunar/teams-service; \
    mkdir -p "$out/bin" "$out/licenses"; \
    go build -trimpath -ldflags="-s -w -X github.com/pocketbase/pocketbase.Version=${POCKETBASE_VERSION}" -o "$out/bin/malrunar-teams-pocketbase" .; \
    "$out/bin/malrunar-teams-pocketbase" --version | tee /dev/stderr | grep -q "${POCKETBASE_VERSION}"; \
    go list -m -f '{{.Path}}@{{.Version}}' all | sort > "$out/THIRD-PARTY.txt"; \
    sed -i '1i Go modules compiled into malrunar-teams-pocketbase (module@version, checksums in pocketbase/go.sum); each module'"'"'s licence file is under licenses/.\n' "$out/THIRD-PARTY.txt"; \
    go list -m -f '{{.Path}} {{.Dir}}' all | while read -r path dir; do \
        [ -n "$dir" ] || continue; \
        for f in "$dir"/LICENSE* "$dir"/COPYING* "$dir"/NOTICE*; do \
            [ -f "$f" ] || continue; \
            install -Dm644 "$f" "$out/licenses/$(printf '%s' "$path" | tr '/' '_')/$(basename "$f")"; \
        done; \
    done; \
    test -s "$out/licenses/github.com_pocketbase_pocketbase/LICENSE.md"; \
    printf 'malrunar-teams-pocketbase: PocketBase v%s (MIT, https://github.com/pocketbase/pocketbase), built from source with go 1.25 from the module versions in pocketbase/go.mod (checksums: pocketbase/go.sum) and the migrations in pocketbase/migrations (Apache-2.0, https://github.com/iarnvidr/malrunar)\n' \
        "${POCKETBASE_VERSION}" > "$out/SOURCE.txt"

FROM scratch AS component
COPY --link --from=pocketbase /out/ /
COPY --link LICENSE README.md package.json /opt/malrunar/teams-service/
COPY --link bin/ /opt/malrunar/teams-service/bin/
COPY --link lib/ /opt/malrunar/teams-service/lib/
COPY --link src/ /opt/malrunar/teams-service/src/
COPY --link dev/ /opt/malrunar/teams-service/dev/
COPY --link etc/ /etc/
