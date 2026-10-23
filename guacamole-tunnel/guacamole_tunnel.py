#!/usr/bin/env python3
"""Let several viewers share ONE Guacamole connection instead of evicting each other.

Guacamole starts a new connection for every viewer, and a desktop that accepts one RDP session
at a time then disconnects the previous viewer. This service sits between a reverse proxy and the
Guacamole web application, in front of its tunnel endpoints only. When a viewer asks to open a
connection (GUAC_TYPE=c) that the same authenticated user already has active, the request is
rewritten into a join (GUAC_TYPE=a) of that session; otherwise it is passed through unchanged and
becomes the session others join.

Who the user is comes from a header that YOUR reverse proxy sets after it has authenticated the
request (and strips from client requests): this service trusts it and nothing else.

Settings (environment):
  MALRUNAR_GUAC_TUNNEL_UPSTREAM       required: base URL of the Guacamole web application,
                                      including its context path, e.g. http://guacamole:8080/guacamole
  MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER required: name of the trusted header that carries the user
                                      name; it is also forwarded to Guacamole under the same name
                                      (which is what Guacamole's header authentication reads)
  MALRUNAR_GUAC_TUNNEL_DATA_SOURCE    optional: Guacamole data source whose active connections are
                                      looked up, default postgresql
  MALRUNAR_GUAC_TUNNEL_PATH_PREFIX    optional: path this service answers under, default the path of
                                      the upstream URL
  MALRUNAR_GUAC_TUNNEL_BIND           optional: listen address, default 127.0.0.1 (0.0.0.0 in a container)
  MALRUNAR_GUAC_TUNNEL_PORT           optional: listen port, default 8082

Routes: <prefix>/websocket-tunnel and <prefix>/tunnel. Request URLs are never logged: Guacamole
carries authentication tokens in them.

`guacamole_tunnel.py --healthcheck` checks that the listening port accepts connections.
"""
import asyncio
import os
import socket
import sys
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from yarl import URL

PREFIX = "MALRUNAR_GUAC_TUNNEL_"
EX_CONFIG = 78

LOCK = web.AppKey('connect_lock', asyncio.Lock)
CONFIG = web.AppKey('config', object)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    upstream: str
    identity_header: str
    data_source: str = 'postgresql'
    path_prefix: str = ''
    bind: str = '127.0.0.1'
    port: int = 8082

    @classmethod
    def from_env(cls, env):
        def get(name, default=None):
            value = env.get(PREFIX + name, '').strip()
            if value:
                return value
            if default is None:
                raise ConfigError(f'{PREFIX}{name} is required')
            return default

        upstream = get('UPSTREAM').rstrip('/')
        parts = urlsplit(upstream)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.query or parts.fragment:
            raise ConfigError(f'{PREFIX}UPSTREAM must be an http(s) URL without query or fragment')
        header = get('IDENTITY_HEADER')
        if not all(c.isalnum() or c == '-' for c in header):
            raise ConfigError(f'{PREFIX}IDENTITY_HEADER must be a header name (letters, digits, hyphen)')
        source = get('DATA_SOURCE', 'postgresql')
        if not all(c.isalnum() or c in '-_.' for c in source):
            raise ConfigError(f'{PREFIX}DATA_SOURCE has characters a data source name cannot have')
        prefix = get('PATH_PREFIX', parts.path).rstrip('/')
        if prefix and not prefix.startswith('/'):
            raise ConfigError(f'{PREFIX}PATH_PREFIX must start with /')
        try:
            port = int(get('PORT', '8082'))
        except ValueError:
            raise ConfigError(f'{PREFIX}PORT must be a number') from None
        if not 1 <= port <= 65535:
            raise ConfigError(f'{PREFIX}PORT must be 1-65535')
        return cls(upstream, header, source, prefix, get('BIND', '127.0.0.1'), port)


async def connection_parameters(session, config, parameters, user):
    """Join only a connectable session owned by the same authenticated user."""
    values = dict(parameters)
    if values.get('GUAC_TYPE') != 'c' or not values.get('token'):
        return parameters
    # Only the configured data source: never construct paths from client input.
    if values.get('GUAC_DATA_SOURCE') != config.data_source:
        return parameters
    url = f'{config.upstream}/api/session/data/{config.data_source}/activeConnections'
    async with session.get(url, headers={'Guacamole-Token': values['token']},
                           timeout=ClientTimeout(total=10)) as response:
        if response.status != 200:
            raise web.HTTPBadGateway(text='Desktop session lookup failed')
        active = await response.json()
    candidates = [item for item in active.values()
                  if item.get('connectable') is True
                  and item.get('username') == user
                  and item.get('connectionIdentifier') == values.get('GUAC_ID')]
    if not candidates:
        return parameters
    existing = min(candidates, key=lambda item: item['startDate'])
    return [(key, existing['identifier'] if key == 'GUAC_ID' else 'a' if key == 'GUAC_TYPE' else value)
            for key, value in parameters]


async def relay(source, target):
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await target.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await target.send_bytes(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            break


async def tunnel(request):
    config = request.app[CONFIG]
    user = request.headers.get(config.identity_header, '').strip()
    if not user:  # no identity to own the session; who may connect is the proxy's decision
        raise web.HTTPForbidden()
    root = config.upstream
    headers = {config.identity_header: user}
    timeout = ClientTimeout(total=None, sock_connect=10)
    async with ClientSession(timeout=timeout, headers=headers) as session:
        if request.path.endswith('/websocket-tunnel'):
            protocols = tuple(filter(None, request.headers.get('Sec-WebSocket-Protocol', '').split(',')))
            # Wait for Guacamole's first instruction before releasing the lock:
            # its active-connection record then exists for simultaneous arrivals.
            try:
                async with request.app[LOCK]:
                    parameters = await connection_parameters(session, config, list(request.query.items()), user)
                    upstream = await session.ws_connect(root + '/websocket-tunnel', params=parameters,
                                                        protocols=protocols, max_msg_size=0)
                    try:
                        first = await asyncio.wait_for(upstream.receive(), 30)
                    except BaseException:
                        await upstream.close()
                        raise
            except (ClientError, OSError, asyncio.TimeoutError):
                raise web.HTTPBadGateway(text='Desktop connection unavailable') from None
            async with upstream:
                downstream = web.WebSocketResponse(protocols=protocols, max_msg_size=0)
                await downstream.prepare(request)
                if first.type == WSMsgType.TEXT:
                    await downstream.send_str(first.data)
                elif first.type == WSMsgType.BINARY:
                    await downstream.send_bytes(first.data)
                else:
                    await downstream.close()
                    return downstream
                tasks = [asyncio.create_task(relay(upstream, downstream)),
                         asyncio.create_task(relay(downstream, upstream))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await downstream.close()
                return downstream

        # Guacamole's HTTP fallback carries connection parameters in its POST
        # body; subsequent reads/writes retain their original tunnel identifier.
        body = await request.read()
        # Tomcat compares the raw query to "connect" exactly. Re-encoding it
        # as form parameters would turn it into "connect=" and break fallback.
        target = URL(root + '/tunnel?' + request.rel_url.raw_query_string, encoded=True)
        headers = {'Content-Type': request.content_type}
        if 'Guacamole-Tunnel-Token' in request.headers:
            headers['Guacamole-Tunnel-Token'] = request.headers['Guacamole-Tunnel-Token']
        if request.method == 'POST' and 'connect' in request.query:
            async with request.app[LOCK]:
                parameters = await connection_parameters(session, config, parse_qsl(body.decode()), user)
                async with session.post(target, data=urlencode(parameters).encode(), headers=headers) as response:
                    response_headers = {'Cache-Control': 'no-store'}
                    if 'Guacamole-Tunnel-Token' in response.headers:
                        response_headers['Guacamole-Tunnel-Token'] = response.headers['Guacamole-Tunnel-Token']
                    return web.Response(status=response.status, body=await response.read(),
                                        content_type=response.content_type, headers=response_headers)
        async with session.request(request.method, target,
                                   data=body, headers=headers) as response:
            downstream = web.StreamResponse(status=response.status,
                                            headers={'Content-Type': response.headers.get('Content-Type', 'application/octet-stream'),
                                                     'Cache-Control': 'no-store'})
            await downstream.prepare(request)
            async for chunk in response.content.iter_any():
                await downstream.write(chunk)
            return downstream


def create_app(config):
    app = web.Application()
    app[LOCK] = asyncio.Lock()
    app[CONFIG] = config
    app.router.add_get(config.path_prefix + '/websocket-tunnel', tunnel)
    app.router.add_route('*', config.path_prefix + '/tunnel', tunnel)
    return app


def healthcheck(config):
    host = '127.0.0.1' if config.bind in ('0.0.0.0', '') else config.bind
    try:
        socket.create_connection((host, config.port), timeout=3).close()
    except OSError:
        return 1
    return 0


def main(argv, env):
    try:
        config = Config.from_env(env)
    except ConfigError as error:
        print(f'guacamole-tunnel: configuration error: {error}', file=sys.stderr)
        return EX_CONFIG
    if '--healthcheck' in argv:
        return healthcheck(config)
    # Never log request URLs: Guacamole carries authentication tokens in them.
    web.run_app(create_app(config), host=config.bind, port=config.port, access_log=None)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:], os.environ))
