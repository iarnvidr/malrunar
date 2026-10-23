// libexec/device-login.mjs against the patched package, with a scripted Microsoft (fetch-mock.mjs) and a
// throwaway HOME: the opt-in gate, the whole device-code flow, and the patch that sends the browser
// Origin header only for the Teams web client.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, '../../libexec/device-login.mjs');
const DIST = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const haveDist = fs.existsSync(path.join(DIST, 'auth', 'session-store.js'));

function run(env, args = []) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'device-login-'));
  const log = path.join(home, 'requests.jsonl');
  fs.writeFileSync(log, '');
  const result = spawnSync(process.execPath, ['--import', path.join(HERE, 'fetch-mock.mjs'), SCRIPT, ...args], {
    env: { PATH: process.env.PATH, HOME: home, LOG_LEVEL: 'error', MOCK_LOG: log, MALRUNAR_MSTEAMS_MCP_PKG: DIST, ...env },
    encoding: 'utf8', timeout: 60000,
  });
  const requests = fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
  return { ...result, home, requests, cleanup: () => fs.rmSync(home, { recursive: true, force: true }) };
}

test('it is off unless MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1, and then contacts nobody', { skip: !haveDist }, () => {
  const off = run({});
  try {
    assert.equal(off.status, 78);
    assert.match(off.stderr, /device-code sign-in is off/);
    assert.equal(off.requests.length, 0, 'no request may be made while it is off');
    for (const value of ['0', 'true', 'yes', '']) {
      const other = run({ MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN: value });
      try { assert.equal(other.status, 78, `value '${value}' must not enable it`); assert.equal(other.requests.length, 0); } finally { other.cleanup(); }
    }
  } finally { off.cleanup(); }
});

test('the device-code flow seeds the session, refreshes it without a browser Origin and adds the region', { skip: !haveDist }, () => {
  const r = run({ MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN: '1' });
  try {
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /USERCODE: ABCD1234/);
    assert.match(r.stdout, /SESSION_SEEDED/);
    assert.match(r.stdout, /REFRESH_OK/);
    assert.match(r.stdout, /REGION emea\/emea01/);
    assert.match(r.stdout, /DONE/);

    const device = r.requests.find(q => q.url.endsWith('/organizations/oauth2/v2.0/devicecode'));
    assert.ok(device, 'the tenant defaults to "organizations"');
    assert.equal(device.params.client_id, '1fec8e78-bce4-4aaf-ab1b-5451cc387264');
    assert.match(device.params.scope, /offline_access/);

    // patches/refresh-origin.cjs: the refresh of a session of another client sends no Origin header
    const refreshes = r.requests.filter(q => q.params.grant_type === 'refresh_token');
    assert.ok(refreshes.length >= 2, 'one refresh per resource scope');
    for (const refresh of refreshes) {
      assert.equal(refresh.params.client_id, '1fec8e78-bce4-4aaf-ab1b-5451cc387264');
      assert.equal(Object.keys(refresh.headers).some(name => name.toLowerCase() === 'origin'), false, 'Origin must not be sent for a non-SPA client');
    }
    // the session store was written (encrypted) in the throwaway home
    assert.ok(fs.existsSync(path.join(r.home, '.teams-mcp-server', 'session-state.json')));
  } finally { r.cleanup(); }
});

test('tenant and client id come from the environment', { skip: !haveDist }, () => {
  const r = run({ MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN: '1', MALRUNAR_MSTEAMS_MCP_TENANT: 'tenant.example.invalid', MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID: '44444444-4444-4444-4444-444444444444' });
  try {
    const device = r.requests.find(q => q.url.endsWith('/oauth2/v2.0/devicecode'));
    assert.ok(device.url.includes('/tenant.example.invalid/'));
    assert.equal(device.params.client_id, '44444444-4444-4444-4444-444444444444');
  } finally { r.cleanup(); }
});

test('the web client id still gets its Origin header on refresh', { skip: !haveDist }, () => {
  const r = run({ MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN: '1', MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID: '5e3ce6c0-2b1f-4285-8d4b-75ee78787346' });
  try {
    const refreshes = r.requests.filter(q => q.params.grant_type === 'refresh_token');
    assert.ok(refreshes.length >= 1);
    for (const refresh of refreshes) assert.equal(refresh.headers.Origin, 'https://teams.microsoft.com');
  } finally { r.cleanup(); }
});
