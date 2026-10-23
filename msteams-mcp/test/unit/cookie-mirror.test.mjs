// libexec/cookie-mirror.mjs against a cookies.sqlite with Firefox's table layout.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { mirrorCookies, microsoftCookies, isMicrosoftHost, sameSiteInt } = await import(path.join(HERE, '../../libexec/cookie-mirror.mjs'));

function profile() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cookie-mirror-'));
  const db = new DatabaseSync(path.join(dir, 'cookies.sqlite'));
  db.exec(`CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', name TEXT, value TEXT,
    host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER,
    inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0, rawSameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0,
    isPartitionedAttributeSet INTEGER DEFAULT 0, updateTime INTEGER,
    CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes))`);
  return { dir, db, rows: () => db.prepare('SELECT * FROM moz_cookies ORDER BY host, name').all() };
}

test('only Microsoft hosts are selected', () => {
  assert.equal(isMicrosoftHost('.login.microsoftonline.com'), true);
  assert.equal(isMicrosoftHost('teams.cloud.microsoft'), true);
  assert.equal(isMicrosoftHost('example.org'), false);
  assert.equal(isMicrosoftHost('notmicrosoft.com'), false);
  assert.equal(isMicrosoftHost('evilmicrosoft.com.example.org'), false);
  assert.deepEqual(microsoftCookies([{ name: 'a', domain: 'example.org' }, { name: 'b', domain: '.office.com' }, { name: '', domain: '.office.com' }]).map(c => c.name), ['b']);
  assert.equal(sameSiteInt('Lax'), 1); assert.equal(sameSiteInt('Strict'), 2); assert.equal(sameSiteInt(undefined), 0);
});

test('cookies are written, replaced by name+host+path, and others are left alone', async () => {
  const p = profile();
  try {
    p.db.prepare("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure, isHttpOnly) VALUES ('', 'ESTSAUTHPERSISTENT', 'old', '.login.microsoftonline.com', '/', 1, 1, 1)").run();
    p.db.prepare("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure, isHttpOnly) VALUES ('', 'keep', 'mine', 'example.org', '/', 1, 0, 0)").run();
    p.db.close();
    const result = await mirrorCookies(p.dir, [
      { name: 'ESTSAUTHPERSISTENT', value: 'new', domain: '.login.microsoftonline.com', path: '/', expires: 2000000000, secure: true, httpOnly: true, sameSite: 'None' },
      { name: 'session-cookie', value: 'v', domain: '.teams.cloud.microsoft', path: '/', expires: -1, secure: true, httpOnly: false, sameSite: 'Lax' },
      { name: 'ignored', value: 'x', domain: 'example.org', path: '/' },
    ], { now: Date.UTC(2026, 0, 1) });
    assert.deepEqual(result, { written: 2 });
    const db = new DatabaseSync(path.join(p.dir, 'cookies.sqlite'));
    const rows = db.prepare('SELECT * FROM moz_cookies ORDER BY host, name').all();
    db.close();
    assert.equal(rows.length, 3);
    const sso = rows.find(r => r.name === 'ESTSAUTHPERSISTENT');
    assert.equal(sso.value, 'new'); assert.equal(sso.expiry, 2000000000); assert.equal(sso.isSecure, 1); assert.equal(sso.isHttpOnly, 1);
    const session = rows.find(r => r.name === 'session-cookie');
    assert.equal(session.sameSite, 1);
    assert.ok(session.expiry > Date.UTC(2026, 0, 1) / 1000, 'a session cookie gets a bounded lifetime');
    assert.equal(rows.find(r => r.name === 'keep').value, 'mine');
    assert.equal(rows.some(r => r.name === 'ignored'), false);
  } finally { fs.rmSync(p.dir, { recursive: true, force: true }); }
});

test('a profile that was never started, and an empty list, are skipped without error', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cookie-mirror-'));
  try {
    assert.match((await mirrorCookies(dir, [{ name: 'a', value: 'b', domain: '.office.com' }])).skipped, /no cookies\.sqlite/);
    assert.match((await mirrorCookies(dir, [])).skipped, /no Microsoft web cookies/);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('a failing write rolls back and leaves the previous cookies', async () => {
  const p = profile();
  try {
    p.db.exec('DROP TABLE moz_cookies; CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, path TEXT, originAttributes TEXT)');
    p.db.prepare("INSERT INTO moz_cookies VALUES ('a', 'old', '.office.com', '/', '')").run();
    p.db.close();
    await assert.rejects(mirrorCookies(p.dir, [{ name: 'a', value: 'new', domain: '.office.com', path: '/' }]));
    const db = new DatabaseSync(path.join(p.dir, 'cookies.sqlite'));
    assert.equal(db.prepare('SELECT value FROM moz_cookies').get().value, 'old');
    db.close();
  } finally { fs.rmSync(p.dir, { recursive: true, force: true }); }
});
