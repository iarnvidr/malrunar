// Forward-auth identity in the HTTP server: the shared vectors against the vendored module, and the
// server's use of it (peer address and proxy secret decide trust; the bearer token is unaffected).
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, utimesSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { IdentityConfig } from '../src/forward-auth.mjs';
import { createServer } from '../src/server.mjs';

const TOKEN = 'x'.repeat(48);
const P = 'MALRUNAR_TEAMS_SERVICE_';
const doc = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'forward-auth-vectors.json'), 'utf8'));
const expand = (value, table) => (typeof value === 'string' && value in table ? 'a'.repeat(table[value]) : value);

test('shared forward-auth vectors pass with the vendored module', async t => {
  assert.ok(doc.cases.length >= 30);
  for (const item of doc.cases) {
    await t.test(item.name, () => {
      const dir = mkdtempSync(join(tmpdir(), 'teams-identity-'));
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
      } finally { rmSync(dir, { recursive: true, force: true }); }
    });
  }
});

async function start(env, { log } = {}) {
  const identity = IdentityConfig.fromEnv(env, P);
  const server = createServer({ token: TOKEN, store: {}, sync: {}, teams: {}, auth: {}, identity, log });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const whoami = async (headers = {}, token = TOKEN) => {
    const response = await fetch(`${base}/whoami`, { headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers } });
    return { status: response.status, body: await response.json() };
  };
  return { whoami, close: () => server.close() };
}

const ADA = { 'Remote-User': 'ada', 'Remote-Name': 'Ada L', 'Remote-Email': 'ada@example.org', 'Remote-Groups': 'dev, admins' };

test('nothing configured: identity headers are ignored (anonymous)', async () => {
  const { whoami, close } = await start({});
  try {
    const { status, body } = await whoami(ADA);
    assert.equal(status, 200);
    assert.deepEqual(body, { user: null, name: null, email: null, groups: [], trusted: false, anonymous: true });
  } finally { close(); }
});

test('trusted proxy address: identity is visible to the handler; X-Forwarded-For does not matter', async () => {
  const { whoami, close } = await start({ [`${P}TRUSTED_PROXIES`]: '127.0.0.0/8, ::1' });
  try {
    const { body } = await whoami({ ...ADA, 'X-Forwarded-For': '203.0.113.9' });
    assert.deepEqual(body, { user: 'ada', name: 'Ada L', email: 'ada@example.org', groups: ['dev', 'admins'], trusted: true, anonymous: false });
  } finally { close(); }
});

test('spoofed headers from an untrusted address are ignored, even when X-Forwarded-For claims a proxy', async () => {
  const { whoami, close } = await start({ [`${P}TRUSTED_PROXIES`]: '198.51.100.0/24' });
  try {
    const { body } = await whoami({ ...ADA, 'X-Forwarded-For': '198.51.100.3' });
    assert.equal(body.anonymous, true);
    assert.equal(body.trusted, false);
  } finally { close(); }
});

test('the bearer token is still required and identity does not replace it', async () => {
  const { whoami, close } = await start({ [`${P}TRUSTED_PROXIES`]: '127.0.0.0/8, ::1' });
  try {
    assert.equal((await whoami(ADA, null)).status, 401);
    assert.equal((await whoami(ADA, 'y'.repeat(48))).status, 401);
  } finally { close(); }
});

test('proxy secret: wrong or missing secret ignored, rotation with two secrets and reload', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'teams-identity-'));
  const file = join(dir, 'secrets');
  const old = 'old-secret-0123456789', next = 'new-secret-0123456789';
  writeFileSync(file, `${old}\n`);
  const { whoami, close } = await start({ [`${P}PROXY_SECRET_HEADER`]: 'X-Proxy-Secret', [`${P}PROXY_SECRET_FILE`]: file });
  try {
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': old })).body.user, 'ada');
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': 'wrong-secret-0123456789' })).body.anonymous, true);
    assert.equal((await whoami(ADA)).body.anonymous, true);
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': next })).body.anonymous, true);
    writeFileSync(file, `${old}\n${next}\n`); // rotation: both accepted
    utimesSync(file, new Date(Date.now() + 5000), new Date(Date.now() + 5000));
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': old })).body.user, 'ada');
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': next })).body.user, 'ada');
    writeFileSync(file, `${next}\n`); // old retired
    utimesSync(file, new Date(Date.now() + 10000), new Date(Date.now() + 10000));
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': old })).body.anonymous, true);
    assert.equal((await whoami({ ...ADA, 'X-Proxy-Secret': next })).body.user, 'ada');
  } finally { close(); rmSync(dir, { recursive: true, force: true }); }
});

test('the request log names the user but never the secret or identity headers', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'teams-identity-'));
  const file = join(dir, 'secrets');
  writeFileSync(file, 'log-secret-0123456789\n');
  const lines = [];
  const { whoami, close } = await start({ [`${P}PROXY_SECRET_HEADER`]: 'X-Proxy-Secret', [`${P}PROXY_SECRET_FILE`]: file }, { log: (level, message) => lines.push(message) });
  try {
    await whoami({ ...ADA, 'X-Proxy-Secret': 'log-secret-0123456789' });
    await new Promise(resolve => setTimeout(resolve, 20));
    const text = lines.join('\n');
    assert.match(text, /GET \/whoami 200 user="ada"/);
    assert.ok(!text.includes('log-secret-0123456789') && !text.includes('ada@example.org') && !text.includes('admins'));
  } finally { close(); rmSync(dir, { recursive: true, force: true }); }
});

test('the startup warning is present only when nothing is trusted', () => {
  assert.match(IdentityConfig.fromEnv({}, P).startupWarning(), /anonymous/);
  assert.equal(IdentityConfig.fromEnv({ [`${P}TRUSTED_PROXIES`]: '127.0.0.1' }, P).startupWarning(), null);
});
