"""Runs vectors.json against forward_auth.py (python3 -m unittest discover -s tools/identity)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forward_auth as fa  # noqa: E402

DOC = json.loads((HERE / 'vectors.json').read_text(encoding='utf-8'))


def expand(value, table):
    if isinstance(value, str) and value in table:
        return 'a' * table[value]
    return value


class Vectors(unittest.TestCase):
    def test_vectors(self):
        self.assertGreaterEqual(len(DOC['cases']), 30)
        for case in DOC['cases']:
            with self.subTest(case['name']), tempfile.TemporaryDirectory() as tmp:
                env = dict(case['config'])
                if 'secret_file_contents' in case:
                    path = Path(tmp, 'secrets')
                    path.write_text(case['secret_file_contents'], encoding='utf-8')
                    env[DOC['prefix'] + 'PROXY_SECRET_FILE'] = str(path)
                table = case.get('expand', {})
                headers = {k: expand(v, table) for k, v in case['request']['headers'].items()}
                expected = {k: (expand(v, table) if not isinstance(v, list) else v) for k, v in case['expected'].items()}
                got = fa.IdentityConfig.from_env(env, DOC['prefix']).resolve(case['request']['peer'], headers)
                self.assertEqual({'user': got.user, 'name': got.name, 'email': got.email,
                                  'groups': list(got.groups), 'trusted': got.trusted}, expected)
                self.assertEqual(got.anonymous, expected['user'] is None)


class Behaviour(unittest.TestCase):
    def secret_file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name, 'secrets')
        path.write_text(text, encoding='utf-8')
        return path

    def test_identity_is_frozen_with_tuple_groups(self):
        ident = fa.Identity(user='a', groups=('x',), trusted=True)
        with self.assertRaises(Exception):
            ident.user = 'b'
        self.assertIsInstance(ident.groups, tuple)
        self.assertTrue(fa.Identity().anonymous)

    def test_startup_warning_only_when_nothing_configured(self):
        self.assertIn('anonymous', fa.IdentityConfig.from_env({}, 'P_').startup_warning())
        self.assertEqual(fa.IdentityConfig.from_env({}, 'P_').mode, 'none')
        self.assertIsNone(fa.IdentityConfig.from_env({'P_TRUSTED_PROXIES': '127.0.0.1'}, 'P_').startup_warning())

    def test_config_errors_name_the_setting(self):
        for env, text in (
            ({'P_TRUSTED_PROXIES': 'nonsense'}, 'P_TRUSTED_PROXIES'),
            ({'P_USER_HEADER': 'bad header'}, 'P_USER_HEADER'),
            ({'P_PROXY_SECRET_HEADER': 'X-S'}, 'P_PROXY_SECRET_FILE'),
            ({'P_PROXY_SECRET_FILE': '/nonexistent/x'}, 'P_PROXY_SECRET_HEADER'),
            ({'P_PROXY_SECRET_HEADER': 'X-S', 'P_PROXY_SECRET_FILE': '/nonexistent/x'}, 'P_PROXY_SECRET_FILE'),
        ):
            with self.subTest(env), self.assertRaises(fa.ConfigError) as caught:
                fa.IdentityConfig.from_env(env, 'P_')
            self.assertIn(text, str(caught.exception))

    def test_secret_file_rules(self):
        env = lambda path: {'P_PROXY_SECRET_HEADER': 'X-S', 'P_PROXY_SECRET_FILE': str(path)}  # noqa: E731
        for text in ('', '# only a comment\n\n', 'short\n', 'aaaaaaaaaaaaaaaa1\naaaaaaaaaaaaaaaa2\naaaaaaaaaaaaaaaa3\n'):
            with self.subTest(text), self.assertRaises(fa.ConfigError):
                fa.IdentityConfig.from_env(env(self.secret_file(text)), 'P_')

    def test_secret_file_is_reread_when_it_changes(self):
        path = self.secret_file('first-secret-0123456\n')
        config = fa.IdentityConfig.from_env({'P_PROXY_SECRET_HEADER': 'X-S', 'P_PROXY_SECRET_FILE': str(path)}, 'P_')
        request = lambda secret: config.resolve('192.0.2.1', {'X-S': secret, 'Remote-User': 'ada'})  # noqa: E731
        self.assertTrue(request('first-secret-0123456').trusted)
        path.write_text('second-secret-012345\n', encoding='utf-8')
        os.utime(path, ns=(1, 1))
        self.assertTrue(request('second-secret-012345').trusted)
        self.assertFalse(request('first-secret-0123456').trusted)
        path.write_text('# being rewritten\n', encoding='utf-8')  # unusable: keep the last good secret
        os.utime(path, ns=(2, 2))
        self.assertTrue(request('second-secret-012345').trusted)

    def test_scrub_headers_and_repr_hide_secrets(self):
        path = self.secret_file('first-secret-0123456\n')
        config = fa.IdentityConfig.from_env({'P_PROXY_SECRET_HEADER': 'X-S', 'P_PROXY_SECRET_FILE': str(path)}, 'P_')
        self.assertEqual(config.scrub_headers({'x-s': 'v', 'REMOTE-USER': 'a', 'Remote-Groups': 'g', 'Accept': '*/*'}), {'Accept': '*/*'})
        self.assertNotIn('first-secret', repr(config))

    def test_from_mapping_accepts_lists(self):
        config = fa.IdentityConfig.from_mapping({'TRUSTED_PROXIES': ['198.51.100.0/24', '::1']})
        self.assertTrue(config.resolve('::1', {'Remote-User': 'a'}).trusted)
        self.assertFalse(config.resolve('203.0.113.1', {'Remote-User': 'a'}).trusted)


if __name__ == '__main__':
    unittest.main()
