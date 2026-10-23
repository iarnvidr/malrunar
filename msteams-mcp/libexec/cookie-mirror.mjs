// Writes Microsoft web cookies into a Firefox profile's cookies.sqlite (Node's built-in node:sqlite).
//
// Only the Microsoft identity and web hosts are written, and each cookie replaces the one with the same
// name, host, path and origin attributes; nothing else in the profile is touched. Firefox must not hold
// the database while this runs: the caller (malrunar-msteams-mcp) stops the browser first.
import { existsSync } from 'node:fs';
import { join } from 'node:path';

export const MS_HOST_SUFFIXES = [
  'login.microsoftonline.com', 'login.windows.net', 'microsoftonline.com',
  'microsoftazuread-sso.com', 'microsoft.com', 'office.com', 'office.net',
  'teams.microsoft.com', 'teams.cloud.microsoft', 'cloud.microsoft',
  'skype.com', 'sharepoint.com', 'live.com', 'msauth.net', 'msftauth.net',
];

export const isMicrosoftHost = (domain) => {
  const host = String(domain).replace(/^\./, '').toLowerCase();
  return MS_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
};

// Firefox sameSite integers: 0 = None, 1 = Lax, 2 = Strict.
export const sameSiteInt = (value) => ({ none: 0, lax: 1, strict: 2 }[String(value || 'none').toLowerCase()] ?? 0);

export function microsoftCookies(cookies) {
  return (cookies || []).filter((cookie) => cookie?.name && cookie?.domain && isMicrosoftHost(cookie.domain));
}

/**
 * @param {string} profileDir  a Firefox profile directory that already has a cookies.sqlite
 * @param {object[]} cookies   Playwright-style cookies ({ name, value, domain, path, expires, secure, httpOnly, sameSite })
 * @returns {{written: number, skipped?: string}}
 */
export async function mirrorCookies(profileDir, cookies, { now = Date.now() } = {}) {
  const selected = microsoftCookies(cookies);
  if (selected.length === 0) return { written: 0, skipped: 'no Microsoft web cookies to mirror' };
  const database = join(profileDir, 'cookies.sqlite');
  if (!existsSync(database)) return { written: 0, skipped: `no cookies.sqlite in ${profileDir} (the profile has not been started yet)` };

  const { DatabaseSync } = await import('node:sqlite');
  const nowMicros = now * 1000; // Firefox stores microseconds since the epoch
  const nowSeconds = Math.floor(now / 1000);
  // A session cookie (expires -1) has no on-disk lifetime in Firefox: give it a bounded one so it
  // survives the restart that re-reads this database.
  const sessionExpiry = nowSeconds + 400 * 24 * 3600;

  const db = new DatabaseSync(database);
  try {
    // Land the write in the main file, not in a -wal a killed Firefox left behind.
    db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
    const remove = db.prepare('DELETE FROM moz_cookies WHERE name = ? AND host = ? AND path = ? AND originAttributes = ?');
    const insert = db.prepare(`INSERT INTO moz_cookies
      (originAttributes, name, value, host, path, expiry, lastAccessed, creationTime,
       isSecure, isHttpOnly, inBrowserElement, sameSite, schemeMap, isPartitionedAttributeSet, updateTime)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    db.exec('BEGIN');
    let written = 0;
    for (const cookie of selected) {
      const path = cookie.path || '/';
      const expiry = (!cookie.expires || cookie.expires < 0) ? sessionExpiry : Math.floor(cookie.expires);
      remove.run(cookie.name, cookie.domain, path, '');
      insert.run('', cookie.name, cookie.value, cookie.domain, path, expiry, nowMicros, nowMicros,
        cookie.secure ? 1 : 0, cookie.httpOnly ? 1 : 0, 0, sameSiteInt(cookie.sameSite), 2 /* https */, 0, nowMicros);
      written += 1;
    }
    db.exec('COMMIT');
    db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
    return { written };
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* not in a transaction */ }
    throw error;
  } finally {
    db.close();
  }
}
