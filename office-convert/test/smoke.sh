#!/usr/bin/env bash
# Smoke test for office-convert: scripts/smoke.sh office-convert [image-ref]
# Runs the unit tests inside the image, starts the service, converts a generated .docx and .pptx
# to PDF, and checks the rejections, the user and the fonts. Every container is time-bounded.
set -euo pipefail
image="${1:?usage: smoke.sh <image-ref>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "office-convert smoke failed: $*" >&2; exit 1; }
run() { timeout -k 10 300 docker run --rm "$@"; }

work="$(mktemp -d)"
chmod 0777 "$work"
cleanup() {
    docker rm -f "office-convert-smoke-$$" "office-convert-smoke-small-$$" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

echo '== user and entrypoint'
test "$(run --entrypoint id "$image" -u)" = 65534 || fail 'the image does not run as uid 65534'
test "$(run --entrypoint id "$image" -g)" = 65534 || fail 'the image does not run as gid 65534'
docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$image" | grep -q -- '--healthcheck' \
    || fail 'no HEALTHCHECK'

echo '== unit tests inside the image (soffice stubbed)'
run --entrypoint python3 -v "$here:/test:ro" -e MALRUNAR_OFFICE_CODE=/opt/malrunar/office-convert \
    "$image" -B -m unittest discover -v -s /test

echo '== configuration errors name the setting'
out="$(run -e MALRUNAR_OFFICE_PORT=nope "$image" 2>&1 || true)"
echo "$out" | grep -q MALRUNAR_OFFICE_PORT || fail "bad port was not reported: $out"

echo '== fonts'
run --entrypoint sh "$image" -c '
    set -eu
    fc-match Calibri | grep -qi carlito
    fc-match Arial | grep -qi liberation
    fc-match "Times New Roman" | grep -qi liberation
    fc-list | grep -qi "DejaVu"
' || fail 'metric-compatible fonts are not configured (Calibri -> Carlito, Arial/Times -> Liberation)'

echo '== generate a docx and a pptx with LibreOffice itself'
cat > "$work/doc.fodt" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
  <office:body><office:text><text:p>malrunar office-convert smoke test</text:p></office:text></office:body>
</office:document>
XML
cat > "$work/deck.fodp" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
  office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">
  <office:body><office:presentation>
    <draw:page draw:name="one">
      <draw:frame svg:x="2cm" svg:y="2cm" svg:width="12cm" svg:height="3cm">
        <draw:text-box><text:p>malrunar office-convert smoke test</text:p></draw:text-box>
      </draw:frame>
    </draw:page>
  </office:presentation></office:body>
</office:document>
XML
run -v "$work:/work" -e HOME=/tmp --entrypoint soffice "$image" \
    --headless --norestore --nolockcheck --nodefault -env:UserInstallation=file:///tmp/gen \
    --convert-to docx --outdir /work /work/doc.fodt >/dev/null
run -v "$work:/work" -e HOME=/tmp --entrypoint soffice "$image" \
    --headless --norestore --nolockcheck --nodefault -env:UserInstallation=file:///tmp/gen \
    --convert-to pptx --outdir /work /work/deck.fodp >/dev/null
test -s "$work/doc.docx" || fail 'could not generate the docx'
test -s "$work/deck.pptx" || fail 'could not generate the pptx'

start() {  # start <name> [docker run options]; prints the host port
    local name="$1"; shift
    timeout -k 10 300 docker run -d --name "$name" -p 127.0.0.1::8090 -e MALRUNAR_OFFICE_BIND=0.0.0.0 "$@" "$image" >/dev/null
    local port
    port="$(docker port "$name" 8090/tcp | head -n1 | sed 's/.*://')"
    for _ in $(seq 1 60); do
        if curl -fsS --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then echo "$port"; return 0; fi
        sleep 1
    done
    docker logs "$name" >&2 || true
    fail "$name did not become healthy"
}

echo '== service'
port="$(start office-convert-smoke-$$)"
test "$(curl -fsS "http://127.0.0.1:$port/health")" = ok || fail '/health is not "ok"'
test "$(docker exec "office-convert-smoke-$$" id -u)" = 65534 || fail 'the service does not run as uid 65534'
for file in doc.docx deck.pptx; do
    curl -fsS --max-time 200 -X POST -H "X-Filename: $file" --data-binary "@$work/$file" \
        -o "$work/$file.pdf" -D "$work/$file.headers" "http://127.0.0.1:$port/convert" \
        || { docker logs "office-convert-smoke-$$" >&2; fail "converting $file failed"; }
    head -c 4 "$work/$file.pdf" | grep -q '^%PDF' || fail "$file did not produce a PDF"
    grep -qi '^content-type: application/pdf' "$work/$file.headers" || fail "$file: wrong content type"
    echo "$file -> $(stat -c %s "$work/$file.pdf") bytes of PDF"
done
# chunked upload takes the other body path
curl -fsS --max-time 200 -X POST -H 'X-Filename: doc.docx' -H 'Transfer-Encoding: chunked' \
    --data-binary "@$work/doc.docx" -o "$work/chunked.pdf" "http://127.0.0.1:$port/convert"
head -c 4 "$work/chunked.pdf" | grep -q '^%PDF' || fail 'chunked upload did not produce a PDF'
code() { curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }
test "$(code -X POST -H 'X-Filename: malware.exe' --data-binary 'x' "http://127.0.0.1:$port/convert")" = 400 \
    || fail 'an unsupported extension was not rejected with 400'
test "$(code -X POST -H 'X-Filename: a.docx' -H 'Transfer-Encoding: chunked' --data-binary 'x' \
    "http://127.0.0.1:$port/convert")" != 200 || fail 'a bogus document was accepted'
test "$(code "http://127.0.0.1:$port/nothing")" = 404 || fail 'unknown path is not 404'

echo '== size limit'
port="$(start "office-convert-smoke-small-$$" -e MALRUNAR_OFFICE_MAX_BYTES=1024)"
head -c 2048 /dev/zero > "$work/big.docx"
test "$(code -X POST -H 'X-Filename: big.docx' --data-binary "@$work/big.docx" "http://127.0.0.1:$port/convert")" = 413 \
    || fail 'an oversize document was not rejected with 413'
test "$(curl -fsS "http://127.0.0.1:$port/health")" = ok || fail 'the service is not healthy after a rejection'

echo 'office-convert smoke passed'
