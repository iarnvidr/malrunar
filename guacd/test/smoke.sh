#!/usr/bin/env bash
# Smoke test for the guacd image: scripts/smoke.sh guacd [image-ref]
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
fail() { echo "guacd smoke failed: $*" >&2; exit 1; }

echo '== version'
docker run --rm --entrypoint /usr/local/sbin/guacd "$image" -v | grep -q '1\.6\.0' || fail 'guacd -v'

echo '== linked packages, libraries and copyright files (run as the image ships: no LD_LIBRARY_PATH, no ldconfig)'
docker run --rm -i --entrypoint bash "$image" -s <<'INNER'
set -euo pipefail
fail() { echo "guacd smoke failed: $*" >&2; exit 1; }
test -s /usr/local/share/malrunar/guacd-linked-packages || fail 'linked package record is missing'
while read -r package version; do
    actual="$(dpkg-query -W -f='${binary:Package} ${Version}' "$package")" || fail "image lacks $package"
    test "$actual" = "$package $version" || fail "linked $package $version but the image has: $actual"
    test -s "/usr/share/doc/${package%%:*}/copyright" || fail "no copyright file for $package"
done < /usr/local/share/malrunar/guacd-linked-packages
for binary in /usr/local/sbin/guacd /usr/local/lib/libguac-client-rdp.so; do
    if ldd "$binary" | grep -q 'not found'; then ldd "$binary" >&2; fail "unresolved libraries for $binary"; fi
done
test -s /usr/local/share/malrunar/licenses/guacamole-server/LICENSE || fail 'guacamole LICENSE missing'
test -z "$(env | grep '^LD_LIBRARY_PATH=' || true)" || fail 'LD_LIBRARY_PATH is set'
INNER

echo '== expected runtime identity and writable HOME'
 docker run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev --tmpfs /home/guacd:rw,nosuid,nodev,uid=1000,gid=1000 --user 1000:1000 "$image" sh -c 'test "$(id -u)" = 1000 && test "$HOME" = /home/guacd && test -w "$HOME" && touch "$HOME/.smoke"' || fail 'uid 1000 cannot write default HOME with read-only root and tmpfs /tmp'

echo '== daemon answers and loads the RDP plugin (dlopen through the rpath)'
name="malrunar-guacd-smoke-$$"
docker run -d --rm --name "$name" "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
reply=""
for _ in $(seq 1 20); do
    reply="$(docker exec "$name" bash -c 'exec 3<>/dev/tcp/127.0.0.1/4822 && printf "6.select,3.rdp;" >&3 && timeout 5 head -c 200 <&3' 2>/dev/null || true)"
    [ -n "$reply" ] && break
    sleep 1
done
case "$reply" in 4.args,*) ;; *) docker logs "$name" >&2 || true; fail "unexpected handshake reply: $reply" ;; esac
echo 'guacd smoke passed'
