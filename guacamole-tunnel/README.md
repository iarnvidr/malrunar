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

## Security: the identity header is trusted

The service believes the user name in the header named by `MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER`.
Your reverse proxy must authenticate the request, **strip that header from client requests** and set it itself,
and only the proxy may reach this service's port (a private network or `127.0.0.1`). Guacamole itself
should be configured to read the same header (header authentication), since the header is forwarded to it
under the same name. Requests without the header, or (if `ALLOWED_USERS` is set) from other users, get `403`.
Request URLs are never logged, as they carry Guacamole tokens.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MALRUNAR_GUAC_TUNNEL_UPSTREAM` | required | Base URL of the Guacamole web application with its context path, e.g. `http://guacamole:8080/guacamole` |
| `MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER` | required | Name of the trusted header carrying the user name. No default on purpose |
| `MALRUNAR_GUAC_TUNNEL_ALLOWED_USERS` | unset | Comma-separated user names allowed to use the tunnel; unset accepts any non-empty identity from the proxy |
| `MALRUNAR_GUAC_TUNNEL_DATA_SOURCE` | `postgresql` | Guacamole data source whose active connections are looked up; requests for other data sources pass through unjoined |
| `MALRUNAR_GUAC_TUNNEL_PATH_PREFIX` | path of `UPSTREAM` | Path under which this service answers (`<prefix>/websocket-tunnel`, `<prefix>/tunnel`) |
| `MALRUNAR_GUAC_TUNNEL_BIND` | `127.0.0.1` (`0.0.0.0` in the image) | Listen address |
| `MALRUNAR_GUAC_TUNNEL_PORT` | `8082` | Listen port |

A missing required or invalid setting stops the service with exit status 78 and a message naming it.
The image runs as uid/gid 65534, keeps no state, and its `HEALTHCHECK` checks that the port accepts connections.

## How to compose

Route only the two tunnel paths to this service and everything else to Guacamole, e.g. with nginx
(the proxy authenticates the user and sets the header; `X-Trusted-User` is your choice of name):

```nginx
location ~ ^/guacamole/(websocket-tunnel|tunnel)$ {
    proxy_set_header X-Trusted-User $authenticated_user;   # set by your authentication; never from the client
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_http_version 1.1;
    proxy_pass http://guacamole-tunnel:8082;
}
location /guacamole/ { proxy_pass http://guacamole:8080; }
```

```yaml
services:
  guacd:                              # ghcr.io/iarnvidr/guacd, unchanged and independent
    image: ghcr.io/iarnvidr/guacd:1.6.0
  guacamole-tunnel:
    image: ghcr.io/iarnvidr/guacamole-tunnel:0.1.0
    environment:
      MALRUNAR_GUAC_TUNNEL_UPSTREAM: http://guacamole:8080/guacamole
      MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER: X-Trusted-User
```

Without this service everything works as before, just with one viewer per desktop at a time.

## Contents

- `/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py`, the service (Python 3 with the distro's `aiohttp`).

Tests: `test/test_tunnel.py` (unit tests against a fake Guacamole, run inside the image by the smoke
test and in pull request CI), `test/e2e_in_container.py` and `test/smoke.sh` (the real process, bounded containers).
Run them with `scripts/build.sh guacamole-tunnel --load && scripts/smoke.sh guacamole-tunnel`.
