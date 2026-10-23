# guacamole-tunnel

Lets several viewers share ONE Guacamole session. `ghcr.io/iarnvidr/guacamole-tunnel`, Apache-2.0.

A remote desktop (an xrdp session, say) usually accepts one RDP connection at a time. Guacamole
opens a new connection for every browser tab, so the second viewer evicts the first, and they keep
evicting each other. This service sits between your reverse proxy and the Guacamole web application,
in front of the two tunnel endpoints (`websocket-tunnel` and the HTTP `tunnel` fallback). When a user
opens a connection that they already have running, the request becomes a *join* of that session
(Guacamole's `GUAC_TYPE=a`); otherwise it passes through unchanged and becomes the session others join.
Only a connectable session of the **same authenticated user** and the **same connection** is joined.

It is opt-in and stands alone: it is a container of its own, it does not change `guacd` (which you run
or not independently), and nothing in it is needed by any other component.

## Dependencies

None from this repository: not `guacd`, not `desktop-base`, not another component. At run time it needs
a Guacamole web application to talk to (yours; the version tested against is the 1.x API) and a reverse
proxy that authenticates users and sets the identity header (below). The smoke test runs the image
with nothing else in it and fails if `guacd` or any other component is present.

## Security: authentication and authorization are external

This service does no authentication or authorization and keeps no allow-list. It reads the user name in the
header named by `MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER` only to keep each user's RDP session separate: a viewer
joins only a session owned by the same identity. **The tunnel must only be reachable through an authenticating
reverse proxy that sets this header and overwrites any client-supplied value**, and only the proxy may reach this
service's port (a private network or `127.0.0.1`). The header is trusted only from the configured proxy addresses
and/or with the shared proxy secret (below); with neither configured nobody is trusted. Deciding who may connect is the proxy's job. Guacamole itself
should be configured to read the same header (header authentication), since the header is forwarded to it
under the same name. A request without the header has no session owner and gets `403`.
Request URLs are never logged, as they carry Guacamole tokens.

## Behind an authenticating proxy

The tunnel does no login of its own; an authenticating reverse proxy (a forward-auth gateway) in front of it
says who the user is in request headers. The header names are configurable and default to the common `Remote-*`
convention that Authelia uses. Authelia is only an example: any proxy that sets these headers works.

| Header (setting) | Default | Use today |
|---|---|---|
| user (`IDENTITY_HEADER`, alias `USER_HEADER`) | `Remote-User` | session ownership; forwarded to Guacamole under this name |
| groups (`GROUPS_HEADER`) | `Remote-Groups` | resolved, **not used** |
| name (`NAME_HEADER`) | `Remote-Name` | resolved, **not used** |
| email (`EMAIL_HEADER`) | `Remote-Email` | resolved, **not used** |

Ownership: a viewer joins only a session owned by the same user; a request without a user gets `403`. There is
no allow-list and no group check here: who may connect is the proxy's decision. Only the user header is
forwarded to Guacamole; the shared secret and the other identity headers are never forwarded. The peer is the
address of the TCP connection, never `X-Forwarded-For`.

**Anti-spoofing.** Identity headers are honoured only if the request really came from your proxy:

- `TRUSTED_PROXIES`: comma-separated CIDRs (IPv4 and IPv6); the connecting address must be in one of them.
- `PROXY_SECRET_HEADER` and `PROXY_SECRET_FILE` (set together): the proxy sends a shared secret in that header.
  The file holds one secret per line, at most two (the old and the new one during a rotation; at least 16
  characters each); it is re-read when it changes, so a rotation needs no restart. Secrets are compared in constant time.
- If both are set, both must pass. If only one is set, that one must pass.
- If neither is set, **nobody is trusted**: every request is anonymous and gets `403`, and the service logs
  a warning at startup saying so.

Example: Authelia and Traefik. The forward-auth service authenticates and authorizes; its `Remote-*` response
headers are copied onto the request (replacing what the client sent), and a second middleware, placed
**after** the forward-auth one in the chain, adds the shared secret, so a client cannot supply it:

```yaml
http:
  middlewares:
    sso:
      forwardAuth:
        address: http://sso:9091/api/authz/forward-auth
        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Name, Remote-Email]
    proxy-secret:
      headers:
        customRequestHeaders:
          X-Proxy-Secret: "the-same-value-as-in-the-secret-file"
  routers:
    guacamole-tunnel:
      rule: Host(`desktop.example.com`) && (Path(`/guacamole/websocket-tunnel`) || Path(`/guacamole/tunnel`))
      middlewares: [sso, proxy-secret]   # order matters: authenticate first, then add the secret
      service: guacamole-tunnel
  services:
    guacamole-tunnel:
      loadBalancer:
        servers: [{url: http://guacamole-tunnel:8082}]
```

with `MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_HEADER=X-Proxy-Secret`, `MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_FILE` pointing
at a mounted file holding the secret, and, if the proxy has a fixed network, `MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES`
set to it as well. Do not commit the secret: mount it from your secret store.

Groups: `Remote-Groups` is parsed and kept with the identity for future use (for example group-based
permissions); nothing in the tunnel acts on it today.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_GUAC_TUNNEL_UPSTREAM` | required | Base URL of the Guacamole web application with its context path, e.g. `http://guacamole:8080/guacamole` |
| `MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER` | `Remote-User` | Name of the header carrying the user name (alias `MALRUNAR_GUAC_TUNNEL_USER_HEADER`); forwarded to Guacamole under this name |
| `MALRUNAR_GUAC_TUNNEL_GROUPS_HEADER` | `Remote-Groups` | Groups header; resolved, not used yet |
| `MALRUNAR_GUAC_TUNNEL_NAME_HEADER` | `Remote-Name` | Display-name header; resolved, not used yet |
| `MALRUNAR_GUAC_TUNNEL_EMAIL_HEADER` | `Remote-Email` | Mail header; resolved, not used yet |
| `MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES` | unset | Comma-separated CIDRs the connecting address must be in for headers to be trusted |
| `MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_HEADER` | unset | Header in which the proxy sends the shared secret; needs `PROXY_SECRET_FILE` |
| `MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_FILE` | unset | File with the secret(s), one per line, at most two (rotation) |
| `MALRUNAR_GUAC_TUNNEL_DATA_SOURCE` | `postgresql` | Guacamole data source whose active connections are looked up; requests for other data sources pass through unjoined |
| `MALRUNAR_GUAC_TUNNEL_PATH_PREFIX` | path of `UPSTREAM` | Path under which this service answers (`<prefix>/websocket-tunnel`, `<prefix>/tunnel`) |
| `MALRUNAR_GUAC_TUNNEL_BIND` | `127.0.0.1` (`0.0.0.0` in the image) | Listen address |
| `MALRUNAR_GUAC_TUNNEL_PORT` | `8082` | Listen port |

With neither `TRUSTED_PROXIES` nor a secret configured nobody is trusted (see above). A missing required or invalid setting stops the service with exit status 78 and a message naming it.
The image runs as uid/gid 65534, keeps no state, and its `HEALTHCHECK` checks that the port accepts connections.

## How to compose

Route only the two tunnel paths to this service and everything else to Guacamole, e.g. with nginx
(the proxy authenticates the user and sets the header; `X-Trusted-User` is your choice of name):

```nginx
location ~ ^/guacamole/(websocket-tunnel|tunnel)$ {
    proxy_set_header X-Trusted-User $authenticated_user;   # set by your authentication; never from the client
    proxy_set_header X-Proxy-Secret $tunnel_proxy_secret;  # the shared secret (PROXY_SECRET_HEADER), from a file include
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_http_version 1.1;
    proxy_pass http://guacamole-tunnel:8082;
}
location /guacamole/ { proxy_pass http://guacamole:8080; }
```

The Traefik example is under "Behind an authenticating proxy" above.

```yaml
services:
  guacd:                              # ghcr.io/iarnvidr/guacd, unchanged and independent
    image: ghcr.io/iarnvidr/guacd:1.6.0
  guacamole-tunnel:
    image: ghcr.io/iarnvidr/guacamole-tunnel:0.1.0
    environment:
      MALRUNAR_GUAC_TUNNEL_UPSTREAM: http://guacamole:8080/guacamole
      MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER: X-Trusted-User
      MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES: 198.51.100.0/24   # your proxy's network
```

Without this service everything works as before, just with one viewer per desktop at a time.

## Contents

- `/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py`, the service (Python 3 with the distro's `aiohttp`), and `forward_auth.py` next to it (the shared, dependency-free identity module, vendored from `tools/identity`).

Tests: `test/test_tunnel.py` (unit tests against a fake Guacamole, run inside the image by the smoke
test and in pull request CI), `test/e2e_in_container.py` and `test/smoke.sh` (the real process, bounded containers).
Run them with `scripts/build.sh guacamole-tunnel --load && scripts/smoke.sh guacamole-tunnel`.
