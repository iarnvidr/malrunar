# thunderbird: Mozilla's official, UNMODIFIED Thunderbird as a component image.
#
# The image is FROM scratch and holds only:
#   /opt/malrunar/thunderbird/app/            the release tarball, unpacked as it is (thunderbird, omni.ja, ...)
#   /opt/malrunar/thunderbird/unmodified.sha256   sha256 of every file of app/, as built: lets anyone check it is untouched
#   /opt/malrunar/thunderbird/SOURCE.txt, LICENSE-MPL-2.0, LICENSE, README.md
#   /opt/malrunar/thunderbird/bin, lib        the launcher (supervisord program) and its helpers
#   /opt/malrunar/thunderbird/consumer/       the changes a consumer may apply to ITS OWN copy at ITS build: patch
#                                             scripts, policy fragments, add-on handling (see README.md). They are
#                                             never applied here.
#   /etc/supervisor/conf.d/malrunar-thunderbird.conf   the supervisord program (CONVENTIONS.md)
# It needs desktop-base (GTK 3, NSS, ALSA, dbus-glib and the X11 libraries Thunderbird links against, python3,
# supervisor, xdpyinfo, setpriv) and nothing else: no node, no other component. Consumers copy it into their
# own image built on desktop-base:
#   COPY --link --from=ghcr.io/iarnvidr/thunderbird:<tag> / /
#
# Mozilla's trademark policy lets the Thunderbird name be used on an UNALTERED build of the latest stable
# release. That is what this image holds: the tarball is verified, never changed, and no policy, add-on,
# preference or omni.ja edit is added to it. Changes belong to the consumer's own image.
#
# Verification (all in the fetch stage, nothing is skipped): SHA256SUMS and SHA256SUMS.asc come from the same
# release directory as the tarball; the signature must be good and made by a subkey of the pinned
# "Mozilla Software Releases" key (the key file is committed, its fingerprint is pinned); the sha256 of the tarball in
# SHA256SUMS must equal the pinned THUNDERBIRD_SHA256, and the tarball must match it.
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG FETCH_APT_SNAPSHOT
RUN --mount=type=bind,source=scripts,target=/scripts \
    export APT_SNAPSHOT="${FETCH_APT_SNAPSHOT}"; \
    . /scripts/apt-snapshot.sh; \
    malrunar_snapshot_enable; \
    malrunar_retry malrunar_apt install -y --no-install-recommends ca-certificates gnupg xz-utils; \
    malrunar_snapshot_disable
ARG THUNDERBIRD_VERSION
ARG THUNDERBIRD_SHA256
ARG MOZILLA_SIGNING_KEY_FINGERPRINT
ADD https://archive.mozilla.org/pub/thunderbird/releases/${THUNDERBIRD_VERSION}/linux-x86_64/en-US/thunderbird-${THUNDERBIRD_VERSION}.tar.xz /dl/thunderbird.tar.xz
ADD https://archive.mozilla.org/pub/thunderbird/releases/${THUNDERBIRD_VERSION}/SHA256SUMS /dl/SHA256SUMS
ADD https://archive.mozilla.org/pub/thunderbird/releases/${THUNDERBIRD_VERSION}/SHA256SUMS.asc /dl/SHA256SUMS.asc
COPY mozilla-release-keys.asc /keys/mozilla-release-keys.asc
RUN export GNUPGHOME="$(mktemp -d)"; chmod 700 "$GNUPGHOME"; \
    gpg --batch --import /keys/mozilla-release-keys.asc; \
    gpg --batch --with-colons --list-keys "${MOZILLA_SIGNING_KEY_FINGERPRINT}" | grep -q "^fpr:::::::::${MOZILLA_SIGNING_KEY_FINGERPRINT}:"; \
    gpg --batch --status-fd 1 --verify /dl/SHA256SUMS.asc /dl/SHA256SUMS | tee /tmp/gpg-status; \
    grep -q '^\[GNUPG:\] GOODSIG ' /tmp/gpg-status; \
    ! grep -qE '^\[GNUPG:\] (BADSIG|EXPSIG|EXPKEYSIG|REVKEYSIG|ERRSIG) ' /tmp/gpg-status; \
    awk -v fpr="${MOZILLA_SIGNING_KEY_FINGERPRINT}" '$2 == "VALIDSIG" && $NF == fpr { ok = 1 } END { exit !ok }' /tmp/gpg-status; \
    wanted="linux-x86_64/en-US/thunderbird-${THUNDERBIRD_VERSION}.tar.xz"; \
    signed="$(awk -v name="$wanted" '$2 == name { print $1 }' /dl/SHA256SUMS)"; \
    [ "$signed" = "${THUNDERBIRD_SHA256}" ] || { echo "signed SHA256SUMS lists '$signed' for $wanted, pinned ${THUNDERBIRD_SHA256}" >&2; exit 1; }; \
    echo "${THUNDERBIRD_SHA256}  /dl/thunderbird.tar.xz" | sha256sum -c -; \
    mkdir -p /out/opt/malrunar/thunderbird; \
    tar -xJf /dl/thunderbird.tar.xz --no-same-owner -C /out/opt/malrunar/thunderbird; \
    mv /out/opt/malrunar/thunderbird/thunderbird /out/opt/malrunar/thunderbird/app; \
    test -x /out/opt/malrunar/thunderbird/app/thunderbird; \
    cd /out/opt/malrunar/thunderbird/app; \
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > ../unmodified.sha256; \
    printf 'Thunderbird %s, the unmodified linux-x86_64 en-US release from Mozilla\nbinary: https://archive.mozilla.org/pub/thunderbird/releases/%s/linux-x86_64/en-US/thunderbird-%s.tar.xz\nsha256: %s (checked against the PGP-signed SHA256SUMS of the release; signing key %s)\nsource: https://archive.mozilla.org/pub/thunderbird/releases/%s/source/ and https://hg.mozilla.org/releases/comm-release/\nlicence: MPL-2.0 (LICENSE-MPL-2.0); about:license inside Thunderbird lists the bundled components\n' \
        "${THUNDERBIRD_VERSION}" "${THUNDERBIRD_VERSION}" "${THUNDERBIRD_VERSION}" "${THUNDERBIRD_SHA256}" "${MOZILLA_SIGNING_KEY_FINGERPRINT}" "${THUNDERBIRD_VERSION}" \
        > ../SOURCE.txt

FROM scratch AS component
COPY --link --from=fetch /out/ /
COPY --link LICENSE LICENSE-MPL-2.0 README.md /opt/malrunar/thunderbird/
COPY --link bin/ /opt/malrunar/thunderbird/bin/
COPY --link lib/ /opt/malrunar/thunderbird/lib/
COPY --link consumer/ /opt/malrunar/thunderbird/consumer/
COPY --link etc/ /etc/
