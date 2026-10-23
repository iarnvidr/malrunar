# office-convert

A small HTTP service that turns Office documents into PDF with LibreOffice headless.
`ghcr.io/iarnvidr/office-convert`, Apache-2.0 (the LibreOffice and font packages keep their own
licences, see [THIRD_PARTY.md](../THIRD_PARTY.md)).

```sh
docker run -d --name office -p 127.0.0.1:8090:8090 -e MALRUNAR_OFFICE_BIND=0.0.0.0 \
  ghcr.io/iarnvidr/office-convert:0.1.0
curl -fsS -X POST -H 'X-Filename: slides.pptx' --data-binary @slides.pptx -o slides.pdf http://127.0.0.1:8090/convert
```

## Security: no authentication of its own

The service has **no authentication** and converts whatever it is sent. By default it listens on
`127.0.0.1` only, which inside a container means nothing can reach it: a container must set
`MALRUNAR_OFFICE_BIND=0.0.0.0`. Publish the port only on a trusted network (for example `-p 127.0.0.1:8090:8090`
or a private Docker network shared with its clients) and put an authenticating proxy in front of it if
that is not enough (see "Behind an authenticating proxy" below). LibreOffice parses untrusted file formats; the image runs it as uid/gid 65534 with
no volumes, kills it on timeout, and uses a fresh profile and temporary directory per request, but
you should still isolate the container (read-only root file system, no extra capabilities, resource limits).

## Behind an authenticating proxy

The service does no authentication and has no per-user state, so it never refuses a request because of
who sent it. If an authenticating reverse proxy (a forward-auth gateway) sits in front of it, the service
reads the user that proxy names and writes it to the request log (`office-convert: <peer> user=<user or -> POST /convert 200`).
The log never contains the document name, the query, the secret or any identity header. `/health` and
`/convert` behave exactly as without a proxy.

The proxy tells the service who the user is in request headers. A client could send the same headers, so they
are trusted only when the request provably came from the proxy:

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_OFFICE_USER_HEADER` | `Remote-User` | Header with the user name |
| `MALRUNAR_OFFICE_GROUPS_HEADER` | `Remote-Groups` | Comma-separated groups |
| `MALRUNAR_OFFICE_NAME_HEADER` | `Remote-Name` | Display name |
| `MALRUNAR_OFFICE_EMAIL_HEADER` | `Remote-Email` | Mail address |
| `MALRUNAR_OFFICE_TRUSTED_PROXIES` | none | Comma-separated CIDRs (IPv4 and IPv6); the connection's peer address must be in one of them |
| `MALRUNAR_OFFICE_PROXY_SECRET_HEADER` | none | Header in which the proxy sends a shared secret |
| `MALRUNAR_OFFICE_PROXY_SECRET_FILE` | none | File with the secret: one per line, at most two (the old and the new one while rotating); blank lines and `#` lines are ignored. Read at start and again whenever the file changes, so a rotation needs no restart |

Anti-spoofing, in the order of preference:

- **Trusted proxy CIDRs** (`..._TRUSTED_PROXIES`): the headers count only when the TCP peer is the proxy. Use the
  proxy's address or its Docker network. Do not trust a range that other clients can reach.
- **Secret header** (`..._PROXY_SECRET_HEADER` plus `..._PROXY_SECRET_FILE`): the proxy adds a secret header
  that clients cannot know; the secret lives in a file (a Docker or Kubernetes secret), not in the environment.
  To rotate, put the new secret on a second line, change the proxy to send it, then delete the old line.
  Secrets are compared in constant time and are at least 16 characters.
- **Both**: both must pass (peer in a trusted CIDR and a valid secret).
- **Neither configured** (the default): no header is trusted, every request is anonymous, and the service logs a
  warning at start. A bad value (a CIDR that does not parse, an unreadable secret file) stops the service with a
  message naming the setting.

Example with Authelia and Traefik (Authelia is only an example: any forward-auth gateway that sets headers
works, and the names below are placeholders). Traefik asks the gateway first, copies its answer headers to the
request, and then a second middleware sets the shared secret, after the gateway, so a client value is overwritten:

```yaml
# Traefik dynamic configuration
http:
  middlewares:
    office-auth:
      forwardAuth:
        address: http://authelia:9091/api/authz/forward-auth
        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Name, Remote-Email]
    office-proxy-secret:
      headers:
        customRequestHeaders:
          X-Proxy-Secret: "<the secret in the file below>"
  routers:
    office:
      rule: Host(`office.example.invalid`)
      middlewares: [office-auth, office-proxy-secret]   # the order matters: secret after forward-auth
      service: office
  services:
    office:
      loadBalancer:
        servers: [{ url: "http://office-convert:8090" }]
```

```sh
# 198.51.100.0/24 stands for the proxy network of your deployment
docker run -d --name office-convert --network proxy-net \
  -e MALRUNAR_OFFICE_BIND=0.0.0.0 \
  -e MALRUNAR_OFFICE_TRUSTED_PROXIES=198.51.100.0/24 \
  -e MALRUNAR_OFFICE_PROXY_SECRET_HEADER=X-Proxy-Secret \
  -e MALRUNAR_OFFICE_PROXY_SECRET_FILE=/run/secrets/office-proxy-secret \
  -v ./office-proxy-secret:/run/secrets/office-proxy-secret:ro \
  ghcr.io/iarnvidr/office-convert:0.1.0
```

Do not publish the container's port directly: a client that reaches it without the proxy is anonymous (when a
trust setting exists) but is still served, because the service has no authentication of its own. The proxy
(or the network) is what keeps unauthenticated users out. Richer integration, for example allowing a
conversion only to members of a group, is possible later: the resolved identity (user, name, email, groups)
is already available to the request handler.

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

The identity settings (`MALRUNAR_OFFICE_USER_HEADER`, `..._TRUSTED_PROXIES`, `..._PROXY_SECRET_HEADER`,
`..._PROXY_SECRET_FILE` and the others) are listed under "Behind an authenticating proxy".

The image `HEALTHCHECK` runs `convert.py --healthcheck` against the configured port.

## Contents

- `/opt/malrunar/office-convert/convert.py`, the service (Python standard library only), and
  `forward_auth.py` next to it, the shared identity module (a vendored copy of `tools/identity/forward_auth.py`;
  edit that one and run `scripts/sync-identity.sh`).
- LibreOffice Impress and Writer filters (`-nogui` packages: no GUI toolkit, no Java), fontconfig, and the
  fonts Liberation, Carlito, Caladea (metric-compatible with Arial/Times, Calibri, Cambria) and DejaVu.

Tests: `test/test_convert.py` (unit tests with `soffice` stubbed, including the identity cases and the shared `test/forward-auth-vectors.json`) and `test/smoke.sh` (builds on a real
LibreOffice: converts a generated .docx and .pptx, checks the rejections, the user and the fonts).
Run them with `scripts/build.sh office-convert --load && scripts/smoke.sh office-convert`.
