import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createCalls } from '../src/calls.mjs';
import { IdentityConfig } from '../src/forward-auth.mjs';
import { createServer } from '../src/server.mjs';

const token = 'private-test-token';
const action = { action: 'join', sessionId: 'current-session', revision: 1 };
const listen = (server, ...address) => new Promise(resolve => server.listen(...address, resolve));
const close = server => new Promise(resolve => server.close(resolve));

test('call routes use only the local bridge and require the verified owner and bearer token', async t => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'ts-call-'));
  const socket = path.join(folder, 'call.sock');
  const seen = [];
  const bridge = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
    seen.push({ path: request.url, body, authorization: request.headers.authorization });
    response.setHeader('Content-Type', 'application/json');
    if (body?.revision === 0) {
      response.statusCode = 409;
      response.end(JSON.stringify({ code: 'stale-session', error: 'private debug context' }));
    } else {
      response.end(JSON.stringify(body ? { ok: true, status: 'executed' } : {
        state: 'prejoin', sessionId: action.sessionId, revision: action.revision,
      }));
    }
  });
  await listen(bridge, socket);
  const server = createServer({ token, calls: createCalls({ socket, owner: 'owner' }),
    identity: new IdentityConfig({ trustedProxies: ['127.0.0.1/32'] }) });
  await listen(server, 0, '127.0.0.1');
  t.after(async () => { await close(server); await close(bridge); await rm(folder, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = (route, user, body, credential = token) => fetch(base + route, {
    method: body ? 'POST' : 'GET',
    headers: { 'Remote-User': user, Authorization: `Bearer ${credential}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  assert.equal((await call('/calls/state', 'owner', undefined, 'wrong')).status, 401);
  assert.equal((await call('/calls/state', 'someone-else')).status, 403);
  assert.equal((await call('/calls/actions', 'owner', { ...action, injectedPath: '/extension/exchange' })).status, 400);
  assert.deepEqual(seen, []);
  assert.equal((await (await call('/calls/state', 'owner')).json()).state, 'prejoin');
  assert.equal((await (await call('/calls/actions', 'owner', action)).json()).status, 'executed');
  const stale = await call('/calls/actions', 'owner', { ...action, revision: 0 });
  assert.equal(stale.status, 409);
  const error = await stale.json();
  assert.equal(error.code, 'stale-session');
  assert.doesNotMatch(error.error, /private debug context/);
  assert.deepEqual(seen.map(x => x.path), ['/state', '/actions', '/actions']);
  assert.ok(seen.every(x => x.authorization === undefined));
});

test('call integration is opt-in and requires an owner and absolute Unix socket', () => {
  assert.equal(createCalls({}), undefined);
  assert.throws(() => createCalls({ socket: 'relative.sock', owner: 'owner' }), /absolute/);
  assert.throws(() => createCalls({ socket: '/tmp/absolute.sock' }), /require an owner/);
});
