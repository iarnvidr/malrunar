// Automated sign-in to Microsoft in the package's own browser, without a person at the screen.
//
// It types the account name and the password into the browser and then waits for YOU to approve
// multi-factor authentication on your phone. On success the browser holds a genuine browser session
// (ESTSAUTH and the persistent single-sign-on cookie). With --cookies-out <file> the Microsoft web
// cookies are also written to that file (mode 0600) so the launcher can mirror them into the visible
// browser's profile (sync-browser-session.mjs). Tenants whose Conditional Access demands a fresh
// sign-in every few days need this repeated on that schedule: that is a Microsoft/tenant boundary.
//
// CREDENTIAL HANDLING. The account (a user principal name) comes from MALRUNAR_MSTEAMS_MCP_UPN; there
// is no default. The password is read from STDIN only: never from an argument or the environment and
// never written to disk, so it stays out of `ps`, `docker inspect` and the container filesystem.
//
// SAFETY. The caller frees the profile lock first when it is shared. This runs headless against the
// package's own profile. It does NOT write the MCP's session-state.json (closeBrowser(manager, false)):
// that file is the MCP's own session, and this browser's snapshot has no Teams tokens.
//
//   browser-login.mjs [--dry-run] [--cookies-out <file>]
//     --dry-run  stop at the password field, submit nothing
// Settings (all optional except the account): MALRUNAR_MSTEAMS_MCP_UPN, MALRUNAR_MSTEAMS_MCP_MFA_TIMEOUT_MS
// (default 240000), MALRUNAR_MSTEAMS_MCP_LOGIN_URL (default https://myaccount.microsoft.com/),
// MALRUNAR_MSTEAMS_MCP_FORCE_BROWSER_LOGIN=1 (clear the Microsoft login cookies first),
// MALRUNAR_MSTEAMS_MCP_PKG (the package's dist directory, set by the launcher).
import { writeFileSync, openSync, closeSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const { createBrowserContext, closeBrowser } = await import(`${PKG}/browser/context.js`);

const UPN = process.env.MALRUNAR_MSTEAMS_MCP_UPN || '';
const MFA_TIMEOUT_MS = Number(process.env.MALRUNAR_MSTEAMS_MCP_MFA_TIMEOUT_MS || 240000);
// My Account forces a real login-server authorize (it has no app-session cookies of its own to
// short-circuit on, unlike Outlook or Teams), so completing it establishes the browser-wide login
// session that then signs every Microsoft web app in. Landing back on its host is the signed-in signal.
const LOGIN_URL = process.env.MALRUNAR_MSTEAMS_MCP_LOGIN_URL || 'https://myaccount.microsoft.com/';
const SUCCESS_HOST = /(^|\.)(myaccount\.microsoft\.com|account\.microsoft\.com|myapplications\.microsoft\.com)$/;
const LOGIN_HOST = /(^|\.)(login\.microsoftonline\.com|login\.live\.com|login\.windows\.net)$/;
const argv = process.argv.slice(2);
const DRYRUN = argv.includes('--dry-run');
const cookiesOutFlag = argv.indexOf('--cookies-out');
const COOKIES_OUT = cookiesOutFlag >= 0 ? argv[cookiesOutFlag + 1] : null;

async function readPasswordFromStdin() {
  if (DRYRUN) return 'dry-run-not-used';
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  // Only the first line; the trailing newline the pipe adds is dropped.
  return Buffer.concat(chunks).toString('utf8').split('\n')[0];
}

const firstVisible = async (page, selectors, timeout) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    for (const selector of selectors) {
      const locator = page.locator(selector).first();
      if (await locator.isVisible().catch(() => false)) return locator;
    }
    await page.waitForTimeout(400);
  }
  return null;
};

async function main() {
  if (!UPN) throw new Error('MALRUNAR_MSTEAMS_MCP_UPN is not set');
  const password = await readPasswordFromStdin();
  const manager = await createBrowserContext({ headless: true });
  const { page } = manager;
  let ok = false;
  try {
    page.setDefaultTimeout(30000);
    // A cached account page can look signed in while Teams rejects its refresh token: an explicit
    // renewal must reach Microsoft authentication.
    if (process.env.MALRUNAR_MSTEAMS_MCP_FORCE_BROWSER_LOGIN === '1') {
      await manager.context.clearCookies({ domain: /(^|\.)microsoftonline\.com$/ });
    }
    console.log(`Signing in ${UPN} ...`);
    // The account page JS-redirects to the login server; the initial load fires before that redirect,
    // so the signed-in state is not judged on the first URL.
    await page.goto(LOGIN_URL, { waitUntil: 'load' });

    // The login server can land on any of three states: the account field (fresh), a "Pick an
    // account" tile for a remembered account, or straight to the password field.
    const entry = await firstVisible(page, [
      'input[type="email"]', '#i0116', 'input[name="loginfmt"]',
      `[data-test-id="${UPN}"]`, `#tilesHolder [role="button"]:has-text("${UPN}")`,
      'input[type="password"]', '#i0118', 'input[name="passwd"]',
    ], 30000);
    if (!entry) {
      await page.waitForTimeout(3000);
      if (SUCCESS_HOST.test(new URL(page.url()).host)) {
        console.log('Already signed in.');
        ok = true;
        return;
      }
      throw new Error(`No sign-in prompt and not on a signed-in page: ${page.url()}`);
    }

    const emailField = page.locator('input[type="email"], #i0116, input[name="loginfmt"]').first();
    if (await emailField.isVisible().catch(() => false)) {
      await emailField.fill(UPN);
      await (await firstVisible(page, ['#idSIButton9', 'input[type="submit"]', 'button:has-text("Next")'], 10000))?.click();
    } else {
      const tile = page.getByText(UPN, { exact: false }).first();
      if (await tile.isVisible().catch(() => false)) await tile.click().catch(() => {});
    }

    const field = await firstVisible(page, ['input[type="password"]', '#i0118', 'input[name="passwd"]'], 30000);
    if (!field) throw new Error('Password field never appeared.');
    if (DRYRUN) {
      console.log('DRYRUN reached password field; not submitting.');
      ok = true;
      return;
    }
    await field.fill(password);
    await (await firstVisible(page, ['#idSIButton9', 'input[type="submit"]', 'button:has-text("Sign in")'], 10000))?.click();

    // Wrong-password and account errors surface fast: fail loudly rather than hang.
    const failure = await firstVisible(page, ['#passwordError', '#usernameError', '.alert-error', '[role="alert"]'], 4000);
    if (failure) {
      const text = (await failure.textContent().catch(() => '') || '').trim();
      if (text) throw new Error(`Sign-in rejected: ${text}`);
    }

    // Multi-factor authentication: show the number-match code (if any) to enter in the
    // authenticator app, then wait for the person to approve.
    const number = await firstVisible(page, ['#idRichContext_DisplaySign', '.display-sign'], 6000);
    if (number) {
      const code = (await number.textContent().catch(() => '') || '').trim();
      if (code) console.log(`MFA_NUMBER ${code}  <-- enter this in the authenticator app`);
    }
    console.log('Waiting for multi-factor approval on your phone...');

    // After approval, clear the "Stay signed in?" prompt. It is matched by BEHAVIOUR, not by text (the
    // page is localised): once approval is given the only login-host page left is that prompt, and
    // submitting its primary button keeps the long-lived session and redirects to the app. The
    // number-match wait page has no submit button, so this never fires early.
    const deadline = Date.now() + MFA_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const host = new URL(page.url()).host;
      if (SUCCESS_HOST.test(host)) { ok = true; break; }
      if (LOGIN_HOST.test(host)) {
        const submit = page.locator('#idSIButton9, input[type="submit"]').first();
        if (await submit.isVisible().catch(() => false)) {
          await submit.click().catch(() => {});
          await page.waitForTimeout(2500);
          continue;
        }
      }
      await page.waitForTimeout(1500);
    }
    if (!ok && SUCCESS_HOST.test(new URL(page.url()).host)) ok = true;
    if (!ok) {
      const shot = join(tmpdir(), 'malrunar-msteams-mcp-login-stuck.png');
      await page.screenshot({ path: shot }).catch(() => {});
      throw new Error(`Timed out; last page: ${page.url()} (screenshot: ${shot})`);
    }
    console.log('Signed in; the browser now has a Microsoft web session.');
    if (COOKIES_OUT) {
      // 0600 from the first byte; the launcher deletes the file after mirroring it.
      closeSync(openSync(COOKIES_OUT, 'w', 0o600));
      writeFileSync(COOKIES_OUT, JSON.stringify(await manager.context.cookies()), { mode: 0o600 });
    }
  } finally {
    // Persist the session into the PROFILE only (closing the context writes it), not into the MCP's
    // session-state.json.
    await closeBrowser(manager, false).catch(() => {});
  }
  if (ok) console.log('DONE Browser signed into Microsoft web.');
}

main().then(() => process.exit(0)).catch((error) => {
  console.error('ERROR', error?.message || error);
  process.exit(1);
});
