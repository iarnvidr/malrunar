#!/usr/bin/env bash
# Smoke test for the obs-runtime image: scripts/smoke.sh obs-runtime [image-ref]
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "obs-runtime smoke failed: $*" >&2; exit 1; }
pin() { grep -h "^$1=" "$here/../pins.env" "$here/../../pins.env" | head -1 | cut -d= -f2-; }
obs_version="$(pin OBS_VERSION)"
upstream="$(pin OBS_UPSTREAM_VERSION)"
cef_version="$(grep '^CEF_VERSION=' "$here/../cef-version.env" | cut -d= -f2-)"
[ -n "$obs_version" ] && [ -n "$upstream" ] && [ -n "$cef_version" ] || fail 'pins missing'
run() { docker run --rm --entrypoint '' "$image" "$@"; }

echo '== dpkg version string is exactly the pinned deb version'
test "$(run dpkg-query -W -f='${Version}' obs-studio)" = "$obs_version" || fail 'dpkg-query obs-studio version'
run test -s /usr/share/doc/obs-studio/copyright || fail 'obs-studio copyright file is missing'

echo '== obs --version equals the pin'
out="$(run xvfb-run -a obs --version 2>&1 | tail -1)"
echo "$out"
case "$out" in *"$upstream"*) ;; *) fail "obs --version '$out' does not contain $upstream" ;; esac

echo '== obs-browser.so and libcef.so are present; the plugin and CEF helpers have no unresolved libraries'
run bash -c '
    set -e
    d=/usr/lib/x86_64-linux-gnu/obs-plugins
    test -r $d/obs-browser.so
    test -r $d/libcef.so
    test -x $d/obs-browser-page
    for f in obs-browser.so libcef.so obs-browser-page libEGL.so libGLESv2.so libvk_swiftshader.so; do
        if ldd "$d/$f" | grep "not found"; then echo "unresolved libraries for $f" >&2; exit 1; fi
    done
' || fail 'obs-browser plugin files or their libraries'

echo '== every OBS plugin and libobs resolves its libraries (reports, fails only for obs-browser and core)'
run bash -c '
    d=/usr/lib/x86_64-linux-gnu/obs-plugins
    for f in $d/*.so /usr/lib/x86_64-linux-gnu/libobs*.so.*; do
        missing="$(ldd "$f" 2>/dev/null | grep "not found" || true)"
        [ -z "$missing" ] || echo "NOTE $(basename "$f"): $missing"
    done
'
run bash -c '
    d=/usr/lib/x86_64-linux-gnu/obs-plugins
    for f in /usr/bin/obs $d/obs-ffmpeg.so $d/obs-x264.so $d/linux-pulseaudio.so $d/linux-capture.so $d/obs-outputs.so $d/image-source.so; do
        if ldd "$f" | grep "not found"; then echo "unresolved libraries for $f" >&2; exit 1; fi
    done
' || fail 'core OBS plugins have unresolved libraries'

echo '== bundled CEF is the version recorded in cef-version.env'
run bash -c "grep -aqF '$cef_version' /usr/lib/x86_64-linux-gnu/obs-plugins/libcef.so" || fail 'libcef.so does not embed the recorded CEF version'
run test -s /usr/local/share/malrunar/licenses/cef/LICENSE.txt || fail 'CEF licence missing'
run test -s /usr/local/share/malrunar/obs-runtime/cef-version.env || fail 'cef-version.env missing'

echo '== copyright files for the OBS library stack'
run bash -c '
    set -e
    for p in libqt6core6t64 libx264-165 libfdk-aac2 libavcodec62 libnss3 libpython3.14; do
        test -s "/usr/share/doc/$p/copyright" || { echo "no copyright file for $p" >&2; exit 1; }
    done
' || fail 'copyright files'

echo '== still a desktop-base: no runtime user, no policies, unmodified firefox, no bound environment'
run bash -c '
    set -e
    test -z "$(awk -F: "\$3 >= 1000 && \$3 != 65534 {print \$1}" /etc/passwd)"
    test ! -e /opt/malrunar/firefox/distribution
    cd /opt/malrunar/firefox
    LC_ALL=C sha256sum -c --quiet /usr/local/share/malrunar/desktop-base/firefox.sha256
    if grep -rqi "Verify-Peer\|Verify-Host" /etc/apt; then exit 1; fi
' || fail 'desktop-base properties lost'
env_names="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" | cut -d= -f1)"
if printf '%s\n' "$env_names" | grep -Eqx 'HOME|DISPLAY|XDG_RUNTIME_DIR|PULSE_SERVER|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY|OBS_.*|QT_X11_NO_MITSHM'; then
    fail "image sets a user- or product-bound environment variable: $(printf '%s\n' "$env_names" | tr '\n' ' ')"
fi

echo '== OBS starts on a virtual display and loads obs-browser'
log="$(docker run --rm --entrypoint '' "$image" bash -c '
    export HOME="$(mktemp -d)"
    xvfb-run -a -s "-screen 0 1280x720x24" timeout -s INT 25 obs --disable-shutdown-check --disable-updater --verbose 2>&1 || true
')"
printf '%s\n' "$log" | grep -q 'OBS 32\|OBS Studio\|obs_init\|Using EGL\|Loading module' || { printf '%s\n' "$log" | tail -30 >&2; fail 'OBS produced no startup log'; }
if printf '%s\n' "$log" | grep -E "Failed to load.*(obs-browser|obs-ffmpeg|obs-x264|linux-capture)|Module '.*(obs-browser|obs-ffmpeg|obs-x264|linux-capture).*' not loaded"; then fail 'a core or browser plugin failed to load'; fi
echo 'obs-runtime smoke passed'
