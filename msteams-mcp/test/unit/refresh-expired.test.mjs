// patches/refresh-expired.cjs: a session whose access tokens have all expired is renewed over HTTP
// instead of being given up on ("No token found in session").
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const haveDist = fs.existsSync(path.join(DIST, 'auth', 'session-store.js'));

test('all access tokens expired: the HTTP refresh runs and succeeds', { skip: !haveDist }, () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'refresh-expired-'));
  const log = path.join(home, 'requests.jsonl');
  fs.writeFileSync(log, '');
  try {
    const result = spawnSync(process.execPath, ['--import', path.join(HERE, 'fetch-mock.mjs'), path.join(HERE, 'refresh-expired.child.mjs')], {
      env: { PATH: process.env.PATH, HOME: home, LOG_LEVEL: 'error', MOCK_LOG: log, MALRUNAR_MSTEAMS_MCP_PKG: DIST }, encoding: 'utf8', timeout: 60000,
    });
    assert.equal(result.status, 0, result.stderr);
    const outcome = JSON.parse(result.stdout.trim().split('\n').pop());
    assert.deepEqual({ ok: outcome.ok, method: outcome.method }, { ok: true, method: 'http' }, JSON.stringify(outcome));
    const refreshes = fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line)).filter(q => q.params.grant_type === 'refresh_token');
    assert.ok(refreshes.length >= 1, 'the refresh token was redeemed');
  } finally { fs.rmSync(home, { recursive: true, force: true }); }
});
