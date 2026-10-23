"""Runs inside the image: the real service process against a fake Guacamole upstream."""
import asyncio
import os
import subprocess
import sys
import tempfile

from aiohttp import ClientSession, web

HEADER = 'X-Trusted-User'
SECRET_HEADER = 'X-Proxy-Secret'
SECRET = 'e2e-secret-0123456789abcdef'
opened = []
upstream_headers = []
active = {}


async def active_connections(request):
    return web.json_response(active)


async def websocket_tunnel(request):
    values = dict(request.query)
    opened.append(values)
    upstream_headers.append(request.headers.copy())
    user = request.headers.get(HEADER)
    identifier = 's1' if user == 'viewer' else 's2'
    if values['GUAC_TYPE'] == 'c':
        active[identifier] = {'identifier': identifier, 'username': user, 'connectionIdentifier': '7',
                              'connectable': True, 'startDate': 1}
    ws = web.WebSocketResponse(protocols=('guacamole',))
    await ws.prepare(request)
    await ws.send_str(f'0.,2.{identifier};')
    async for message in ws:
        await ws.send_str(message.data)
    return ws


async def main():
    fake = web.Application()
    fake.router.add_get('/gw/api/session/data/postgresql/activeConnections', active_connections)
    fake.router.add_get('/gw/websocket-tunnel', websocket_tunnel)
    runner = web.AppRunner(fake)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18080).start()
    secret_dir = tempfile.TemporaryDirectory()
    secret_file = os.path.join(secret_dir.name, 'secret')
    with open(secret_file, 'w') as handle:
        handle.write(SECRET + '\n')
    # The client below connects from 127.0.0.1, so trust that and require the shared secret as well.
    env = dict(os.environ, MALRUNAR_GUAC_TUNNEL_UPSTREAM='http://127.0.0.1:18080/gw',
               MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER=HEADER,
               MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES='127.0.0.0/8',
               MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_HEADER=SECRET_HEADER,
               MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_FILE=secret_file,
               MALRUNAR_GUAC_TUNNEL_PORT='18082')
    service = subprocess.Popen([sys.executable, '-I', '/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py'], env=env)
    try:
        async with ClientSession() as http:
            for _ in range(50):
                try:
                    async with http.get('http://127.0.0.1:18082/gw/websocket-tunnel') as response:
                        assert response.status == 403, response.status  # no identity, no secret
                    break
                except OSError:
                    await asyncio.sleep(0.2)
            else:
                raise SystemExit('the service did not start')
            assert subprocess.run([sys.executable, '-I', '/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py',
                                   '--healthcheck'], env=env).returncode == 0
            params = {'token': 't', 'GUAC_DATA_SOURCE': 'postgresql', 'GUAC_ID': '7', 'GUAC_TYPE': 'c'}
            trusted = {SECRET_HEADER: SECRET, 'Remote-Groups': 'admins'}
            sockets = []
            for _ in range(2):
                ws = await http.ws_connect('http://127.0.0.1:18082/gw/websocket-tunnel', params=params,
                                           headers={HEADER: 'viewer'} | trusted, protocols=('guacamole',))
                assert (await ws.receive()).data == '0.,2.s1;'
                await ws.send_str('4.sync,1.1;')
                assert (await ws.receive()).data == '4.sync,1.1;'
                sockets.append(ws)
            assert [o['GUAC_TYPE'] for o in opened] == ['c', 'a'], opened
            assert opened[1]['GUAC_ID'] == 's1', opened
            # Another user, valid proxy: never joins viewer's session, the upstream is asked for its own.
            other = await http.ws_connect('http://127.0.0.1:18082/gw/websocket-tunnel', params=params,
                                          headers={HEADER: 'stranger'} | trusted, protocols=('guacamole',))
            assert (await other.receive()).data == '0.,2.s2;'
            assert opened[2]['GUAC_TYPE'] == 'c' and opened[2]['GUAC_ID'] == '7', opened
            sockets.append(other)
            # Missing or wrong secret: 403 and nothing reaches the upstream.
            for headers in ({HEADER: 'viewer'}, {HEADER: 'viewer', SECRET_HEADER: 'wrong-secret-0123456789'}):
                async with http.get('http://127.0.0.1:18082/gw/websocket-tunnel', params=params,
                                    headers=headers) as response:
                    assert response.status == 403, response.status
            assert len(opened) == 3, opened
            for sent in upstream_headers:
                assert SECRET_HEADER not in sent and 'Remote-Groups' not in sent, dict(sent)
            for ws in sockets:
                await ws.close()
        print('guacamole-tunnel e2e: second viewer joined the first viewer session, other user and untrusted requests did not')
    finally:
        service.terminate()
        service.wait(10)
        await runner.cleanup()
        secret_dir.cleanup()


asyncio.run(main())
