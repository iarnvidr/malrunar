#!/usr/bin/env bash
# Smoke test for the desktop-base image: scripts/smoke.sh desktop-base [image-ref]
# Needs docker, curl, tar, sha256sum and network access (it downloads the pinned Firefox tarball
# to compare the installed tree with it).
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "desktop-base smoke failed: $*" >&2; exit 1; }
pin() { grep -h "^$1=" "$here/../pins.env" "$here/../../pins.env" | head -1 | cut -d= -f2-; }
firefox_version="$(pin FIREFOX_ESR_VERSION)"
firefox_sha256="$(pin FIREFOX_ESR_SHA256)"
[ -n "$firefox_version" ] && [ -n "$firefox_sha256" ] || fail 'pins missing'
run() { timeout -k 10 300 docker run --rm --entrypoint '' "$image" "$@"; }

echo '== firefox --version equals the pin'
out="$(run /opt/malrunar/firefox/firefox --version)"
echo "$out"
test "$out" = "Mozilla Firefox ${firefox_version}" || test "$out" = "Mozilla Firefox ${firefox_version%esr}" \
    || fail "firefox reports '$out', pinned $firefox_version"

echo '== firefox tree is exactly the official tarball (files, modes, links), nothing added'
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 5 --retry-all-errors \
    "https://ftp.mozilla.org/pub/firefox/releases/${firefox_version}/linux-x86_64/en-US/firefox-${firefox_version}.tar.xz" \
    -o "$work/firefox.tar.xz"
echo "${firefox_sha256}  $work/firefox.tar.xz" | sha256sum -c - >/dev/null || fail 'downloaded tarball does not match the pinned sha256'
mkdir "$work/ref"
tar -xJf "$work/firefox.tar.xz" -C "$work/ref"
( cd "$work/ref/firefox" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$work/ref.sha256"
( cd "$work/ref/firefox" && find . -mindepth 1 -printf '%y %m %l %p\n' | LC_ALL=C sort -k4 ) > "$work/ref.tree"
run bash -c 'cd /opt/malrunar/firefox && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum' > "$work/img.sha256"
run bash -c 'cd /opt/malrunar/firefox && find . -mindepth 1 -printf "%y %m %l %p\n" | LC_ALL=C sort -k4' > "$work/img.tree"
test -s "$work/img.sha256" || fail 'no files found under /opt/malrunar/firefox'
diff "$work/ref.sha256" "$work/img.sha256" || fail 'firefox file contents differ from the tarball'
diff "$work/ref.tree" "$work/img.tree" || fail 'firefox file tree (modes, links, extra or missing paths) differs from the tarball'
# The build-time record must still match: a derived layer that changed Firefox would break it.
run bash -c 'cd /opt/malrunar/firefox && LC_ALL=C sha256sum -c --quiet /usr/local/share/malrunar/desktop-base/firefox.sha256' \
    || fail 'recorded firefox checksums do not verify'
run bash -c 'test "$(find /opt/malrunar/firefox ! -user root | wc -l)" = 0' || fail 'firefox files are not owned by root'

echo '== no policies, distribution directory, autoconfig, extensions'
run bash -c '
    set -e
    # The official tarball ships firefox/distribution/distribution.ini (from Mozilla); nothing else may be there.
    test "$(cd /opt/malrunar/firefox/distribution && find . -mindepth 1 | sort)" = "./distribution.ini"
    test ! -e /opt/malrunar/firefox/policies.json
    test ! -e /opt/malrunar/firefox/defaults/pref/autoconfig.js
    test ! -e /opt/malrunar/firefox/mozilla.cfg
    test ! -e /opt/malrunar/firefox/browser/extensions
    test ! -e /etc/firefox
    test -z "$(find / -xdev -name policies.json -path "*irefox*" 2>/dev/null)"
    test -z "$(find / -xdev -type d -name distribution -path "*irefox*" ! -path /opt/malrunar/firefox/distribution 2>/dev/null)"
' || fail 'a policies/distribution/autoconfig/extension path exists'
run bash -c 'test ! -e /usr/local/bin/firefox && test ! -e /opt/firefox' || fail 'compat symlink or old firefox path present'

echo '== xrdp, xorgxrdp, pipewire, wireplumber, supervisor, tini, ffmpeg'
run bash -c '
    set -e
    for p in xrdp xorgxrdp pipewire pipewire-pulse wireplumber supervisor tini ffmpeg xfce4-session; do
        dpkg-query -W -f="\${Package} \${Version}\n" "$p"
        test -s "/usr/share/doc/$p/copyright"
    done
    test -x /usr/sbin/xrdp
    test -x /usr/sbin/xrdp-sesman
    test -x /usr/bin/tini
    test -x /usr/bin/supervisord
    test -n "$(dpkg -L xorgxrdp | grep "\.so$")"
' || fail 'a base package or its copyright file is missing'

echo '== pipewire-module-xrdp is present, links cleanly and carries its licence'
run bash -c '
    set -e
    m=/usr/lib/x86_64-linux-gnu/pipewire-0.3/libpipewire-module-xrdp.so
    test -s "$m"
    if ldd "$m" | grep -q "not found"; then ldd "$m"; exit 1; fi
    test -s /usr/local/share/malrunar/licenses/pipewire-module-xrdp/LICENSE
' || fail 'pipewire-module-xrdp'

echo '== no runtime user, no user-bound environment'
run bash -c '
    set -e
    if getent passwd ubuntu || getent group ubuntu; then exit 1; fi
    test -z "$(awk -F: "\$3 >= 1000 && \$3 != 65534 {print \$1}" /etc/passwd)"
    test -z "$(awk -F: "\$3 >= 1000 && \$3 != 65534 {print \$1}" /etc/group)"
    test ! -e /home/ubuntu
    id -nG xrdp | tr " " "\n" | grep -qx ssl-cert
' || fail 'a non-system user or group exists (or xrdp is not in ssl-cert)'
env_names="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" | cut -d= -f1)"
if printf '%s\n' "$env_names" | grep -Eqx 'HOME|DISPLAY|XDG_RUNTIME_DIR|PULSE_SERVER|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY|OBS_.*|MOZ_.*'; then
    fail "image sets a user- or product-bound environment variable: $(printf '%s\n' "$env_names" | tr '\n' ' ')"
fi
test "$(docker image inspect --format '{{.Config.User}}{{len .Config.Entrypoint}}{{len .Config.ExposedPorts}}' "$image")" = '00' \
    || fail 'image sets USER, ENTRYPOINT or EXPOSE'

echo '== apt setup is TLS-verified and restored'
run bash -c '
    set -e
    if grep -rqi "Verify-Peer\|Verify-Host" /etc/apt; then exit 1; fi
    grep -rq "archive.ubuntu.com" /etc/apt/sources.list.d
    if grep -rq snapshot /etc/apt/sources.list.d; then exit 1; fi
    test -s /etc/ssl/certs/ca-certificates.crt
    test -z "$(ls -A /var/lib/apt/lists | grep -v "^partial$\|^lock$")"
    test -s /usr/local/share/malrunar/lib/apt-snapshot.sh
    . /usr/local/share/malrunar/desktop-base/build-info.env
    test -n "$APT_SNAPSHOT"
' || fail 'apt configuration'
echo 'desktop-base smoke passed'
