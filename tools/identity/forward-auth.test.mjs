// Runs vectors.json against forward-auth.mjs (node --test tools/identity/forward-auth.test.mjs).
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { ANONYMOUS, ConfigError, IdentityConfig } from './forward-auth.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const doc = JSON.parse(readFileSync(join(here, 'vectors.json'), 'utf8'));
const expand = (value, table) => (typeof value === 'string' && value in table ? 'a'.repeat(table[value]) : value);
const tmp = () => mkdtempSync(join(tmpdir(), 'forward-auth-'));

test('vectors', async t => {
  assert.ok(doc.cases.length >= 30);
  for (const item of doc.cases) {
    await t.test(item.name, () => {
      const dir = tmp();
      try {
        const env = { ...item.config };
        if ('secret_file_contents' in item) {
          const path = join(dir, 'secrets');
          writeFileSync(path, item.secret_file_contents);
          env[`${doc.prefix}PROXY_SECRET_FILE`] = path;
        }
        const table = item.expand || {};
        const headers = Object.fromEntries(Object.entries(item.request.headers).map(([k, v]) => [k, expand(v, table)]));
        const expected = Object.fromEntries(Object.entries(item.expected).map(([k, v]) => [k, Array.isArray(v) ? v : expand(v, table)]));
        const got = IdentityConfig.fromEnv(env, doc.prefix).resolve(item.request.peer, headers);
        assert.deepEqual({ user: got.user, name: got.name, email: got.email, groups: [...got.groups], trusted: got.trusted }, expected);
        assert.equal(got.anonymous, expected.user === null);
      } finally { rmSync(dir, { recursive: true, force: true }); }
    });
  }
});

test('anonymous identity is frozen', () => {
  assert.ok(ANONYMOUS.anonymous);
  assert.throws(() => { 'use strict'; ANONYMOUS.user = 'x'; });
  assert.throws(() => { 'use strict'; ANONYMOUS.groups.push('x'); });
});

test('startup warning only when nothing is configured', () => {
  assert.match(IdentityConfig.fromEnv({}, 'P_').startupWarning(), /anonymous/);
  assert.equal(IdentityConfig.fromEnv({}, 'P_').mode, 'none');
  assert.equal(IdentityConfig.fromEnv({ P_TRUSTED_PROXIES: '127.0.0.1' }, 'P_').startupWarning(), null);
});

test('configuration errors name the setting', () => {
  const cases = [
    [{ P_TRUSTED_PROXIES: 'nonsense' }, 'P_TRUSTED_PROXIES'],
    [{ P_TRUSTED_PROXIES: '198.51.100.0/33' }, 'P_TRUSTED_PROXIES'],
    [{ P_USER_HEADER: 'bad header' }, 'P_USER_HEADER'],
    [{ P_PROXY_SECRET_HEADER: 'X-S' }, 'P_PROXY_SECRET_FILE'],
    [{ P_PROXY_SECRET_FILE: '/nonexistent/x' }, 'P_PROXY_SECRET_HEADER'],
    [{ P_PROXY_SECRET_HEADER: 'X-S', P_PROXY_SECRET_FILE: '/nonexistent/x' }, 'P_PROXY_SECRET_FILE'],
  ];
  for (const [env, text] of cases) assert.throws(() => IdentityConfig.fromEnv(env, 'P_'), error => error instanceof ConfigError && error.message.includes(text));
});

test('secret file rules, reload and scrubbing', () => {
  const dir = tmp();
  try {
    const path = join(dir, 'secrets');
    for (const text of ['', '# only a comment\n\n', 'short\n', 'aaaaaaaaaaaaaaaa1\naaaaaaaaaaaaaaaa2\naaaaaaaaaaaaaaaa3\n']) {
      writeFileSync(path, text);
      assert.throws(() => IdentityConfig.fromEnv({ P_PROXY_SECRET_HEADER: 'X-S', P_PROXY_SECRET_FILE: path }, 'P_'), ConfigError);
    }
    writeFileSync(path, 'first-secret-0123456\n');
    const config = IdentityConfig.fromEnv({ P_PROXY_SECRET_HEADER: 'X-S', P_PROXY_SECRET_FILE: path }, 'P_');
    const request = secret => config.resolve('192.0.2.1', { 'x-s': secret, 'remote-user': 'ada' });
    assert.ok(request('first-secret-0123456').trusted);
    writeFileSync(path, 'second-secret-012345\n');
    utimesSync(path, 1, 1);
    assert.ok(request('second-secret-012345').trusted);
    assert.ok(!request('first-secret-0123456').trusted);
    writeFileSync(path, '# being rewritten\n');
    utimesSync(path, 2, 2);
    assert.ok(request('second-secret-012345').trusted);
    assert.deepEqual(config.scrubHeaders({ 'x-s': 'v', 'REMOTE-USER': 'a', 'Remote-Groups': 'g', Accept: '*/*' }), { Accept: '*/*' });
    assert.ok(!JSON.stringify(config).includes('first-secret'));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('duplicate header values arrive as arrays and do not match a secret', () => {
  const config = IdentityConfig.fromObject({ TRUSTED_PROXIES: ['192.0.2.0/24'] });
  assert.equal(config.resolve('192.0.2.1', { 'remote-user': ['ada', 'bob'] }).user, 'ada, bob');
});
