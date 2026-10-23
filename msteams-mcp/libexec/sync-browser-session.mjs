// Mirror the Microsoft web session of msteams-mcp into the VISIBLE browser's profile, so one
// sign-in covers both the MCP and the on-screen browser with no interaction in the browser.
//
// msteams-mcp keeps two stores:
//   1. session-state.json: a Playwright storageState (JSON). It holds the cookie jar of the sign-in
//      (ESTSAUTHPERSISTENT on login.microsoftonline.com plus Teams, Outlook and SharePoint cookies)
//      and powers the MCP's tools over HTTPS;
//   2. a Firefox profile of its own, which the package's browser uses.
// The visible browser (the work-browser component) has a third, different profile. This script copies
// store 1's cookie jar into that profile's cookies.sqlite, ONE WAY. ESTSAUTHPERSISTENT is the broad
// Microsoft single-sign-on cookie: once it is in the profile, Microsoft web apps sign the browser in
// silently at the next load. Cookies only, on purpose: Teams' MSAL cache in localStorage/IndexedDB is
// Firefox's private format and is not touched.
//
// Limits: whether Microsoft web apps accept the cookie without asking again depends on the tenant's
// Conditional Access policy; that is a Microsoft/tenant boundary this cannot bypass.
//
//   sync-browser-session.mjs [--dry-run] [--cookies-file <json>]
//     --cookies-file  read a JSON array of cookies from that file instead of session-state.json
// Settings: MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR (the visible browser's profile, set by the launcher),
// MALRUNAR_MSTEAMS_MCP_PKG (the package's dist directory).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { mirrorCookies, microsoftCookies } from './cookie-mirror.mjs';

const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const PROFILE_DIR = process.env.MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR
  || process.env.MALRUNAR_WORK_BROWSER_PROFILE_DIR
  || join(homedir(), '.config', 'malrunar-work-browser', 'profile');
const argv = process.argv.slice(2);
const DRY_RUN = argv.includes('--dry-run');
const fileFlag = argv.indexOf('--cookies-file');
const COOKIES_FILE = fileFlag >= 0 ? argv[fileFlag + 1] : null;

async function main() {
  let cookies;
  if (COOKIES_FILE) {
    cookies = JSON.parse(readFileSync(COOKIES_FILE, 'utf8'));
  } else {
    const { readSessionState } = await import(`${PKG}/auth/session-store.js`);
    cookies = readSessionState()?.cookies || [];
  }
  const selected = microsoftCookies(cookies);
  if (selected.length === 0) {
    console.log('SYNC_SKIP no Microsoft web cookies in the session to mirror.');
    return 0;
  }
  const hosts = [...new Set(selected.map((c) => c.domain))].sort();
  console.log(`Mirroring ${selected.length} Microsoft cookies into ${PROFILE_DIR}`);
  console.log(`Hosts: ${hosts.join(', ')}`);
  const sso = selected.find((c) => c.name === 'ESTSAUTHPERSISTENT');
  console.log(sso
    ? 'Broad single-sign-on cookie ESTSAUTHPERSISTENT present.'
    : 'WARNING no ESTSAUTHPERSISTENT cookie; browser-wide single sign-on may not hold.');
  if (DRY_RUN) {
    console.log('DRY_RUN no changes written.');
    return 0;
  }
  const result = await mirrorCookies(PROFILE_DIR, selected);
  if (result.skipped) {
    console.log(`SYNC_SKIP ${result.skipped}.`);
    return 0;
  }
  console.log(`SYNC_DONE wrote ${result.written} Microsoft cookies to the visible browser's profile.`);
  return 0;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error('SYNC_ERROR', error?.message || error);
  process.exit(1);
});
