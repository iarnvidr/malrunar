import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

CODE = Path(os.environ.get('MALRUNAR_GUAC_TUNNEL_CODE', Path(__file__).resolve().parent.parent))
spec = importlib.util.spec_from_file_location('guacamole_tunnel', CODE / 'guacamole_tunnel.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


HEADER = 'X-Trusted-User'
SECRET_HEADER = 'X-Proxy-Secret'
SECRET = 'secret-number-one-0123456789'
NEW_SECRET = 'secret-number-two-0123456789'


class TunnelBase(AioHTTPTestCase):
    async def get_application(self):
        self.active = {}
        self.opened = []
        self.http_connections = []
        self.upstream_headers = []
        fake = web.Application()

        async def active(request):
            if request.headers.get('Guacamole-Token') != 'test-token':
                raise web.HTTPForbidden()
            return web.json_response(self.active)

        async def socket(request):
            values = dict(request.query)
            self.opened.append(values)
            if values['GUAC_TYPE'] == 'c':
                # Simulate a slow initial connection and publish its record
                # only once ready, exercising simultaneous first arrivals.
                await asyncio.sleep(0.05)
                self.active['shared'] = {'identifier': 'shared', 'username': 'viewer',
                                         'connectionIdentifier': '1', 'connectable': True, 'startDate': 1}
            ws = web.WebSocketResponse(protocols=('guacamole',))
            await ws.prepare(request)
            await ws.send_str('0.,6.shared;')
            async for message in ws:
                await ws.send_str(message.data)
            return ws

        async def http(request):
            if 'connect' in request.query:
                self.assertEqual(request.rel_url.raw_query_string, 'connect')
                self.http_connections.append(parse_qs(await request.text()))
                return web.Response(text='http-tunnel-id', headers={'Guacamole-Tunnel-Token': 'test-http-token'})
            self.assertEqual(request.rel_url.raw_query_string, 'read:http-tunnel-id:0')
            self.assertEqual(request.headers.get('Guacamole-Tunnel-Token'), 'test-http-token')
            return web.Response(text='4.sync,1.1;')

        async def record(request):
            self.upstream_headers.append(request.headers.copy())
            return await socket(request)

        fake.router.add_get('/guac/api/session/data/postgresql/activeConnections', active)
        fake.router.add_get('/guac/websocket-tunnel', record)
        fake.router.add_route('*', '/guac/tunnel', http)
        self.upstream = TestServer(fake)
        await self.upstream.start_server()
        return proxy.create_app(self.config())

    def config(self):
        env = {'MALRUNAR_GUAC_TUNNEL_UPSTREAM': str(self.upstream.make_url('/guac')),
               'MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER': HEADER}
        env.update(self.extra_env())
        return proxy.Config.from_env(env)

    def extra_env(self):
        # The test client connects from 127.0.0.1.
        return {'MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES': '127.0.0.0/8'}

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.upstream.close()

    def parameters(self):
        return {'token': 'test-token', 'GUAC_DATA_SOURCE': 'postgresql', 'GUAC_ID': '1', 'GUAC_TYPE': 'c'}

    async def open(self):
        return await self.client.ws_connect('/guac/websocket-tunnel', params=self.parameters(),
                                             headers={HEADER: 'viewer'}, protocols=('guacamole',))



class TunnelTests(TunnelBase):
    async def test_simultaneous_viewers_share_one_transport_and_relay_both_directions(self):
        first, second = await asyncio.gather(self.open(), self.open())
        try:
            self.assertEqual([item['GUAC_TYPE'] for item in self.opened], ['c', 'a'])
            self.assertEqual(self.opened[1]['GUAC_ID'], 'shared')
            for ws in (first, second):
                self.assertEqual(ws.protocol, 'guacamole')
                self.assertEqual((await ws.receive()).data, '0.,6.shared;')
                await ws.send_str('4.sync,1.1;')
                self.assertEqual((await ws.receive()).data, '4.sync,1.1;')
        finally:
            await first.close()
            await second.close()

    async def test_other_users_and_connections_are_never_joined(self):
        self.active = {
            'other-user': {'username': 'other', 'connectionIdentifier': '1', 'connectable': True},
            'other-desktop': {'username': 'viewer', 'connectionIdentifier': '2', 'connectable': True},
            'not-connectable': {'username': 'viewer', 'connectionIdentifier': '1', 'connectable': False},
        }
        ws = await self.open()
        await ws.close()
        self.assertEqual(self.opened[0]['GUAC_TYPE'], 'c')

    async def test_authentication_failure_does_not_create_a_new_transport(self):
        parameters = self.parameters() | {'token': 'invalid'}
        response = await self.client.get('/guac/websocket-tunnel', params=parameters,
                                         headers={HEADER: 'viewer'})
        self.assertEqual(response.status, 502)
        self.assertEqual(self.opened, [])
        self.assertNotIn('invalid', await response.text())

    async def test_untrusted_request_is_rejected(self):
        response = await self.client.get('/guac/websocket-tunnel', params=self.parameters())
        self.assertEqual(response.status, 403)
        self.assertEqual(self.opened, [])

    async def test_http_fallback_joins_same_session(self):
        ws = await self.open()
        try:
            response = await self.client.post('/guac/tunnel?connect', data=self.parameters(),
                                               headers={HEADER: 'viewer'})
            self.assertEqual(await response.text(), 'http-tunnel-id')
            self.assertEqual(response.headers['Guacamole-Tunnel-Token'], 'test-http-token')
            self.assertEqual(self.http_connections[-1]['GUAC_TYPE'], ['a'])
            self.assertEqual(self.http_connections[-1]['GUAC_ID'], ['shared'])
            response = await self.client.get('/guac/tunnel?read:http-tunnel-id:0',
                                              headers={HEADER: 'viewer', 'Guacamole-Tunnel-Token': 'test-http-token'})
            self.assertEqual(await response.text(), '4.sync,1.1;')
        finally:
            await ws.close()

    async def test_other_identity_gets_its_own_session_and_never_joins_anothers(self):
        first = await self.open()  # 'viewer' owns the session 'shared'
        try:
            second = await self.client.ws_connect('/guac/websocket-tunnel', params=self.parameters(),
                                                  headers={HEADER: 'other'}, protocols=('guacamole',))
            await second.close()
            self.assertEqual([o['GUAC_TYPE'] for o in self.opened], ['c', 'c'])
            self.assertNotIn('shared', [o['GUAC_ID'] for o in self.opened])
        finally:
            await first.close()

    async def test_headers_are_not_forwarded_except_the_user_header(self):
        ws = await self.client.ws_connect('/guac/websocket-tunnel', params=self.parameters(), protocols=('guacamole',),
                                          headers={HEADER: 'viewer', 'Remote-Groups': 'admins', 'Remote-Email': 'v@x.test',
                                                   'Remote-Name': 'V', SECRET_HEADER: SECRET})
        await ws.close()
        sent = self.upstream_headers[0]
        self.assertEqual(sent[HEADER], 'viewer')
        for name in ('Remote-Groups', 'Remote-Email', 'Remote-Name', 'Remote-User', SECRET_HEADER):
            self.assertNotIn(name, sent)

    async def test_blank_identity_header_is_rejected(self):
        response = await self.client.get('/guac/websocket-tunnel', params=self.parameters(),
                                         headers={HEADER: '  '})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.opened, [])

    async def test_other_identity_header_is_not_trusted(self):
        response = await self.client.get('/guac/websocket-tunnel', params=self.parameters(),
                                         headers={'Remote-User': 'viewer'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.opened, [])


class SpoofedPeerTests(TunnelBase):
    """A peer outside the trusted CIDRs is anonymous whatever it claims."""
    def extra_env(self):
        return {'MALRUNAR_GUAC_TUNNEL_TRUSTED_PROXIES': '198.51.100.0/24'}

    async def test_spoofed_headers_from_untrusted_address_are_rejected(self):
        spoof = {HEADER: 'viewer', 'X-Forwarded-For': '198.51.100.5', 'Forwarded': 'for=198.51.100.5'}
        response = await self.client.get('/guac/websocket-tunnel', params=self.parameters(), headers=spoof)
        self.assertEqual(response.status, 403)
        response = await self.client.post('/guac/tunnel?connect', data=self.parameters(), headers=spoof)
        self.assertEqual(response.status, 403)
        self.assertEqual((self.opened, self.http_connections), ([], []))


class NothingConfiguredTests(TunnelBase):
    def config(self):
        env = {'MALRUNAR_GUAC_TUNNEL_UPSTREAM': str(self.upstream.make_url('/guac')),
               'MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER': HEADER}
        return proxy.Config.from_env(env)

    async def test_nobody_is_trusted_without_configuration(self):
        response = await self.client.get('/guac/websocket-tunnel', params=self.parameters(), headers={HEADER: 'viewer'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.opened, [])
        self.assertIn('every request is anonymous', proxy.Config.from_env(
            {'MALRUNAR_GUAC_TUNNEL_UPSTREAM': 'http://x/guac'}).identity.startup_warning())


class SecretTests(TunnelBase):
    def extra_env(self):
        self.secret_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.secret_dir.cleanup)
        self.secret_file = Path(self.secret_dir.name) / 'secret'
        self.secret_file.write_text(f'{SECRET}\n{NEW_SECRET}\n')
        return {'MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_HEADER': SECRET_HEADER,
                'MALRUNAR_GUAC_TUNNEL_PROXY_SECRET_FILE': str(self.secret_file)}

    async def test_missing_or_wrong_secret_is_rejected(self):
        for secret in (None, 'wrong-secret-0123456789'):
            response = await self.client.get('/guac/websocket-tunnel', params=self.parameters(),
                                             headers={HEADER: 'viewer'} | ({SECRET_HEADER: secret} if secret else {}))
            self.assertEqual(response.status, 403)
        self.assertEqual(self.opened, [])

    async def test_either_of_two_secrets_is_accepted_during_rotation(self):
        for secret in (SECRET, NEW_SECRET):
            ws = await self.client.ws_connect('/guac/websocket-tunnel', params=self.parameters(), protocols=('guacamole',),
                                              headers={HEADER: 'viewer', SECRET_HEADER: secret})
            await ws.close()
        self.assertEqual(len(self.opened), 2)
        self.assertTrue(all(SECRET_HEADER not in sent for sent in self.upstream_headers))


class ConfigTests(unittest.TestCase):
    BASE = {'MALRUNAR_GUAC_TUNNEL_UPSTREAM': 'http://guacamole:8080/guacamole/',
            'MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER': 'X-Trusted-User'}

    def test_identity_header_defaults_to_remote_user(self):
        env = {k: v for k, v in self.BASE.items() if k != 'MALRUNAR_GUAC_TUNNEL_IDENTITY_HEADER'}
        self.assertEqual(proxy.Config.from_env(env).identity_header, 'Remote-User')

    def test_defaults_and_derived_prefix(self):
        config = proxy.Config.from_env(self.BASE)
        self.assertEqual((config.upstream, config.path_prefix, config.data_source, config.port, config.bind),
                         ('http://guacamole:8080/guacamole', '/guacamole', 'postgresql', 8082, '127.0.0.1'))

    def test_required_settings_are_named(self):
        env = {k: v for k, v in self.BASE.items() if k != 'MALRUNAR_GUAC_TUNNEL_UPSTREAM'}
        with self.assertRaisesRegex(proxy.ConfigError, 'MALRUNAR_GUAC_TUNNEL_UPSTREAM'):
            proxy.Config.from_env(env)

    def test_bad_values_are_named(self):
        for name, value in (('PORT', 'x'), ('PORT', '70000'), ('IDENTITY_HEADER', 'a b'), ('TRUSTED_PROXIES', 'nonsense'), ('PROXY_SECRET_HEADER', 'X-S'), ('UPSTREAM', 'ftp://x'),
                            ('PATH_PREFIX', 'no-slash'), ('DATA_SOURCE', 'a/b')):
            env = dict(self.BASE, **{'MALRUNAR_GUAC_TUNNEL_' + name: value})
            # The shared module names the setting USER_HEADER (IDENTITY_HEADER is its older alias).
            with self.assertRaisesRegex(proxy.ConfigError, 'USER_HEADER' if name == 'IDENTITY_HEADER' else name):
                proxy.Config.from_env(env)

    def test_main_reports_configuration_error_with_status_78(self):
        self.assertEqual(proxy.main([], {}), 78)


if __name__ == '__main__':
    unittest.main()
