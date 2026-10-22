"""Runs inside the image: the real service process against a fake Guacamole upstream."""
import asyncio
import os
import subprocess
import sys

from aiohttp import ClientSession, web

HEADER = 'X-Trusted-User'
opened = []
active = {}


async def active_connections(request):
    return web.json_response(active)


async def websocket_tunnel(request):
    values = dict(request.query)
    opened.append(values)
    if values['GUAC_TYPE'] == 'c':
        active['s1'] = {'identifier': 's1', 'username': 'viewer', 'connectionIdentifier': '7',
                        'connectable': True, 'startDate': 1}
    ws = web.WebSocketResponse(protocols=('guacamole',))
    await ws.prepare(request)
    await ws.send_str('0.,2.s1;')
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
    env = dict(os.environ, MALRUNAR_GUAC_TUNNEL_UPSTREAM='http://127.0.0.1:18080/gw',
               MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER=HEADER, MALRUNAR_GUAC_TUNNEL_ALLOWED_USERS='viewer',
               MALRUNAR_GUAC_TUNNEL_PORT='18082')
    service = subprocess.Popen([sys.executable, '-I', '/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py'], env=env)
    try:
        async with ClientSession() as http:
            for _ in range(50):
                try:
                    async with http.get('http://127.0.0.1:18082/gw/websocket-tunnel') as response:
                        assert response.status == 403, response.status  # no identity header
                    break
                except OSError:
                    await asyncio.sleep(0.2)
            else:
                raise SystemExit('the service did not start')
            assert subprocess.run([sys.executable, '-I', '/opt/malrunar/guacamole-tunnel/guacamole_tunnel.py',
                                   '--healthcheck'], env=env).returncode == 0
            params = {'token': 't', 'GUAC_DATA_SOURCE': 'postgresql', 'GUAC_ID': '7', 'GUAC_TYPE': 'c'}
            sockets = []
            for _ in range(2):
                ws = await http.ws_connect('http://127.0.0.1:18082/gw/websocket-tunnel', params=params,
                                           headers={HEADER: 'viewer'}, protocols=('guacamole',))
                assert (await ws.receive()).data == '0.,2.s1;'
                await ws.send_str('4.sync,1.1;')
                assert (await ws.receive()).data == '4.sync,1.1;'
                sockets.append(ws)
            assert [o['GUAC_TYPE'] for o in opened] == ['c', 'a'], opened
            assert opened[1]['GUAC_ID'] == 's1', opened
            async with http.get('http://127.0.0.1:18082/gw/websocket-tunnel', params=params,
                                headers={HEADER: 'stranger'}) as response:
                assert response.status == 403
            for ws in sockets:
                await ws.close()
        print('guacamole-tunnel e2e: second viewer joined the first viewer session')
    finally:
        service.terminate()
        service.wait(10)
        await runner.cleanup()


asyncio.run(main())
