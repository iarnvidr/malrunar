# office-convert

A small HTTP service that turns Office documents into PDF with LibreOffice headless.
`ghcr.io/iarnvidr/office-convert`, Apache-2.0 (the LibreOffice and font packages keep their own
licences, see [THIRD_PARTY.md](../THIRD_PARTY.md)).

```sh
docker run -d --name office -p 127.0.0.1:8090:8090 -e MALRUNAR_OFFICE_BIND=0.0.0.0 \
  ghcr.io/iarnvidr/office-convert:0.1.0
curl -fsS -X POST -H 'X-Filename: slides.pptx' --data-binary @slides.pptx -o slides.pdf http://127.0.0.1:8090/convert
```

## Security: no authentication

The service has **no authentication** and converts whatever it is sent. By default it listens on
`127.0.0.1` only, which inside a container means nothing can reach it: a container must set
`MALRUNAR_OFFICE_BIND=0.0.0.0`. Publish the port only on a trusted network (for example `-p 127.0.0.1:8090:8090`
or a private Docker network shared with its clients) and put an authenticating proxy in front of it if
that is not enough. LibreOffice parses untrusted file formats; the image runs it as uid/gid 65534 with
no volumes, kills it on timeout, and uses a fresh profile and temporary directory per request, but
you should still isolate the container (read-only root file system, no extra capabilities, resource limits).

## API

| Request | Answer |
|---|---|
| `GET /health` | `200 ok`; always answered, also while a conversion runs |
| `POST /convert` | The body is the document, `X-Filename` names it. `200` with `application/pdf` |

Only the extension of `X-Filename` is used (case-insensitive): `.pptx .ppt .odp .key .docx .doc .odt`.
The body is sent with `Content-Length` or `Transfer-Encoding: chunked`.

| Status | Meaning |
|---|---|
| 400 | Unsupported or missing extension, malformed `Content-Length`, chunk size or chunk framing, empty body, unsupported `Transfer-Encoding` |
| 404 | Unknown path |
| 411 | Neither `Content-Length` nor chunked encoding |
| 413 | Document larger than `MALRUNAR_OFFICE_MAX_BYTES` (decided from the announced size, before the body is read) |
| 422 | LibreOffice ran but produced no PDF |
| 500 | LibreOffice could not be started |
| 504 | Conversion exceeded `MALRUNAR_OFFICE_TIMEOUT_SECONDS`; LibreOffice and all its child processes were killed |

One conversion runs at a time; further requests wait for their turn (the server is multi-threaded, so
`/health` is never blocked). Nothing is kept between requests, and document names are never logged.
PNG/slide image output is not provided.

## Configuration reference

All optional, read at start; a bad value stops the service with a message naming the setting.

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_OFFICE_PORT` | `8090` | Listen port (1-65535) |
| `MALRUNAR_OFFICE_BIND` | `127.0.0.1` | Listen address. Set `0.0.0.0` in a container; see Security |
| `MALRUNAR_OFFICE_TIMEOUT_SECONDS` | `180` | Time limit for one conversion, in seconds (1-86400) |
| `MALRUNAR_OFFICE_MAX_BYTES` | `209715200` | Largest accepted document (200 MiB) |

The image `HEALTHCHECK` runs `convert.py --healthcheck` against the configured port.

## Contents

- `/opt/malrunar/office-convert/convert.py`, the service (Python standard library only).
- LibreOffice Impress and Writer filters (`-nogui` packages: no GUI toolkit, no Java), fontconfig, and the
  fonts Liberation, Carlito, Caladea (metric-compatible with Arial/Times, Calibri, Cambria) and DejaVu.

Tests: `test/test_convert.py` (unit tests with `soffice` stubbed) and `test/smoke.sh` (builds on a real
LibreOffice: converts a generated .docx and .pptx, checks the rejections, the user and the fonts).
Run them with `scripts/build.sh office-convert --load && scripts/smoke.sh office-convert`.
