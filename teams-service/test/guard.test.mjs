// Proves the Teams service cannot change msteams-mcp's session files, using
// msteams-mcp's real refresh code against a throwaway session in a temp HOME.
// Needs the library: set MALRUNAR_TEAMS_SERVICE_MCP_DIST (the image has it at
// /opt/malrunar/msteams-mcp/app/dist); the tests are skipped without it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = process.env.MALRUNAR_TEAMS_SERVICE_MCP_DIST || '/opt/malrunar/msteams-mcp/app/dist';
const haveLibrary = fs.existsSync(path.join(DIST, 'auth', 'session-store.js'));

function child(script, home, ...args) {
  const result = spawnSync(process.execPath, [path.join(HERE, script), ...args], {
    env: { ...process.env, HOME: home, MALRUNAR_TEAMS_SERVICE_MCP_DIST: DIST, LOG_LEVEL: 'error' }, encoding: 'utf8'
  });
  if (result.status !== 0) throw new Error(`${script} failed: ${result.stderr || result.stdout}`);
  return result.stdout.trim().split('\n').pop();
}

const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

test('library code can refresh tokens but nothing reaches the MCP session files', { skip: !haveLibrary }, () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'teams-guard-'));
  try {
    const dir = path.join(home, '.teams-mcp-server');
    fs.mkdirSync(dir, { recursive: true });
    child('child-mcp-write.mjs', home, '60');
    const file = path.join(dir, 'session-state.json');
    const before = sha(file);
    const mtimeBefore = fs.statSync(file).mtimeMs;
    const filesBefore = fs.readdirSync(dir).sort();

    const result = JSON.parse(child('child-guarded.mjs', home, 'refresh'));
    assert.equal(result.ok, true);
    assert.equal(result.rotated, true, 'the rotated refresh token is visible to the library in memory');
    assert.ok(result.tokenCalls >= 3);
    assert.deepEqual(result.refused, []);
    assert.equal(result.promised, 'EGUARDED');
    assert.ok(result.overlay.includes('session-state.json'));
    assert.ok(result.remainingMin > 50);

    assert.equal(sha(file), before, 'session-state.json bytes changed');
    assert.equal(fs.statSync(file).mtimeMs, mtimeBefore, 'session-state.json mtime changed');
    assert.deepEqual(fs.readdirSync(dir).sort(), filesBefore, 'a file appeared in the MCP state directory');
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('a newer file written by the MCP replaces the in-memory tokens', { skip: !haveLibrary }, () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'teams-guard-'));
  try {
    fs.mkdirSync(path.join(home, '.teams-mcp-server'), { recursive: true });
    const result = JSON.parse(child('child-guarded.mjs', home, 'adopt'));
    assert.equal(result.first, true);
    assert.ok(result.a > 100 && result.a <= 120, `first read ${result.a} minutes`);
    assert.equal(result.tokenCalls, 0, 'fresh tokens in the file need no refresh');
    assert.ok(result.b > 160, `expected the newer file's tokens, got ${result.b} minutes`);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('the session key derivation is computed once per key', async () => {
  const { memoiseKeyDerivation } = await import('../src/lib.mjs');
  const crypto = await import('node:crypto');
  memoiseKeyDerivation();
  memoiseKeyDerivation();
  const started = process.hrtime.bigint();
  const first = crypto.scryptSync('host:user', 'salt-for-test', 32);
  const afterFirst = process.hrtime.bigint();
  const second = crypto.scryptSync('host:user', 'salt-for-test', 32);
  const afterSecond = process.hrtime.bigint();
  assert.deepEqual(first, second);
  assert.notEqual(first, second, 'callers get their own copy of the key');
  assert.ok(afterSecond - afterFirst < (afterFirst - started) / 5n, 'the second derivation is a lookup');
  assert.notDeepEqual(crypto.scryptSync('other:user', 'salt-for-test', 32), first);
});
