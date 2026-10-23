# Draft upstream PRs for m0nkmaster/msteams-mcp

These are **drafts**: five fixes that a downstream deployment currently carries as build-time patches against the compiled `dist/` JavaScript of an older upstream commit (a69069cd3f4852122de2db0beb1a58c51876fccd, npm 0.27.2). Each was re-examined against the TypeScript source of **v0.30.2** (HEAD cb0a165) and, where still relevant, re-implemented as a real source change on its own branch with tests. They are meant to be filed by the repository owner. **Nothing has been filed**: no pull requests, issues or comments were created, and nothing was pushed anywhere. The branches exist only in a local clone. Each diff below is the exact `git diff v0.30.2..<branch>` of its branch, and each was checked with `git apply --check` against a clean v0.30.2 checkout.

Base version for all branches: `v0.30.2` (cb0a165). Branch names refer to a local clone of the upstream repository.

| # | Fix | Still needed at v0.30.2 | Branch |
|---|-----|-------------------------|--------|
| 1 | Choose Firefox (or Chromium) via `MSTEAMS_MCP_BROWSER`, optional executable path | Yes | `fix/firefox-browser` |
| 2 | Configurable login wait (`MSTEAMS_LOGIN_TIMEOUT_MS`); skip doomed headless attempt for `forceNew` | Partly: configurable timeout yes; headless skip already fixed upstream (#51) | `fix/login-timeout` (timeout part only) |
| 3 | Run HTTP refresh when every access token has expired | No, fixed upstream (#28) | none |
| 4 | Send browser `Origin` header only for the Teams web SPA client id | Yes | `fix/refresh-origin` |
| 5 | Substrate token: prime search box, accept `teams.cloud.microsoft` and other Teams hosts for messaging cookies, optional login URL | Partly: login-host detection and wait-for-live-token already upstream (#47, #51); priming, cookie hosts and URL override still needed | `fix/substrate-token` |

Verification summary (each branch: `npm ci --ignore-scripts && npm run build && npx vitest run && npx eslint src/`):

| Branch | build | vitest | eslint |
|--------|-------|--------|--------|
| baseline v0.30.2 | ok | 26 files, 557 tests passed | clean |
| `fix/firefox-browser` | ok | 27 files, 562 tests passed | clean |
| `fix/login-timeout` | ok | 27 files, 564 tests passed | clean |
| `fix/refresh-origin` | ok | 26 files, 559 tests passed | clean |
| `fix/substrate-token` | ok | 27 files, 564 tests passed | clean |

Caveat that applies to all of them: nothing here was exercised against a live Microsoft tenant in this environment. Browser launching (Firefox) and the live Teams UI behaviour (search-box priming, host redirects) are covered only by unit tests with mocked Playwright pages and by the observations described in each Problem section.

---

## 1. Select the browser engine (Chromium or Firefox)

**Suggested PR title:** `feat(browser): choose Chromium or Firefox via MSTEAMS_MCP_BROWSER`

### Problem

`src/browser/context.ts` imports only `chromium` from Playwright and always launches the `chrome` channel (`msedge` on Windows). Users who cannot or do not want to run Chrome/Edge (for example, a Linux container that already ships Firefox, or an environment where Firefox shares a single-sign-on session with other applications) have no way to use another browser. Without Chrome/Edge installed, the launch fails with the existing "Could not launch Google Chrome" error and there is no alternative.

An important constraint, learned the hard way: Playwright drives Firefox over its own "juggler" protocol, which **only Playwright's own patched Firefox build implements**. Pointing Playwright at a stock Mozilla Firefox (for example the distribution package) does not work: the browser ignores the juggler pipe and the launch hangs until Playwright kills it (observed as a launch timeout with a fresh profile, whereas Playwright's bundled Firefox launched and loaded Teams normally).

### Change

- New `MSTEAMS_MCP_BROWSER` environment variable: `chromium` (default, behaviour unchanged) or `firefox`. Any other value fails with a clear error.
- New optional `MSTEAMS_MCP_BROWSER_EXECUTABLE`: path to the browser executable (for Chromium it replaces the channel).
- `getBrowserSettings()` (exported, pure) resolves the settings; `createBrowserContext()` picks `chromium` or `firefox` from Playwright accordingly and adjusts the launch-failure message.
- README documents both variables and states that Firefox requires Playwright's own build (`npx playwright install firefox`).
- Unit tests for the resolver.

### Diff

```diff
diff --git a/README.md b/README.md
index 5f9402c..e16d279 100644
--- a/README.md
+++ b/README.md
@@ -86,6 +86,8 @@ Then configure your MCP client:
 
 The server uses your system's Chrome (macOS/Linux) or Edge (Windows) for authentication.
 
+To use Firefox instead, set `MSTEAMS_MCP_BROWSER=firefox` and install Playwright's Firefox build with `npx playwright install firefox`. Playwright drives Firefox over the juggler protocol, which only its own patched Firefox build implements, so a stock Mozilla Firefox install will not work. Optionally set `MSTEAMS_MCP_BROWSER_EXECUTABLE` to the path of the browser executable (for Chromium this replaces the Chrome/Edge channel). `MSTEAMS_MCP_BROWSER` defaults to `chromium`, which keeps the behaviour described above.
+
 ### CLI
 
 The same functionality is also available as a standalone command-line tool, `msteams`. This is useful when you want Teams from a shell or script instead of an MCP client.
diff --git a/src/browser/context.test.ts b/src/browser/context.test.ts
new file mode 100644
index 0000000..4f5b558
--- /dev/null
+++ b/src/browser/context.test.ts
@@ -0,0 +1,31 @@
+import { describe, it, expect } from 'vitest';
+import { getBrowserSettings } from './context.js';
+
+describe('getBrowserSettings', () => {
+  it('defaults to the Chrome channel on non-Windows platforms', () => {
+    expect(getBrowserSettings({}, 'linux')).toEqual({
+      engine: 'chromium', channel: 'chrome', executablePath: undefined,
+    });
+  });
+
+  it('defaults to the Edge channel on Windows', () => {
+    expect(getBrowserSettings({}, 'win32').channel).toBe('msedge');
+  });
+
+  it('selects Firefox without a Chromium channel', () => {
+    const settings = getBrowserSettings({ MSTEAMS_MCP_BROWSER: 'Firefox' }, 'linux');
+    expect(settings).toEqual({ engine: 'firefox', executablePath: undefined });
+  });
+
+  it('passes through an explicit executable path', () => {
+    const settings = getBrowserSettings({
+      MSTEAMS_MCP_BROWSER: 'firefox',
+      MSTEAMS_MCP_BROWSER_EXECUTABLE: '/opt/pw/firefox/firefox',
+    }, 'linux');
+    expect(settings.executablePath).toBe('/opt/pw/firefox/firefox');
+  });
+
+  it('rejects unknown browsers', () => {
+    expect(() => getBrowserSettings({ MSTEAMS_MCP_BROWSER: 'safari' }, 'linux')).toThrow(/Unsupported MSTEAMS_MCP_BROWSER/);
+  });
+});
diff --git a/src/browser/context.ts b/src/browser/context.ts
index 63aafeb..116d2db 100644
--- a/src/browser/context.ts
+++ b/src/browser/context.ts
@@ -13,7 +13,7 @@
  * - No need for storageState temp files or encrypted session restoration for browser use
  */
 
-import { chromium, type BrowserContext, type Page } from 'playwright';
+import { chromium, firefox, type BrowserContext, type BrowserType, type Page } from 'playwright';
 import * as fs from 'fs';
 import * as path from 'path';
 import {
@@ -113,22 +113,64 @@ function cleanupStaleSingletonLock(): boolean {
   return false;
 }
 
+/** Browser engines the login/refresh flows can drive. */
+export type BrowserEngine = 'chromium' | 'firefox';
+
+/** Resolved browser launch settings. */
+export interface BrowserSettings {
+  engine: BrowserEngine;
+  /** Chromium only: installed-browser channel ('msedge' | 'chrome'). */
+  channel?: 'msedge' | 'chrome';
+  /** Explicit browser executable, if configured. */
+  executablePath?: string;
+}
+
 /**
- * Determines the browser channel to use based on the platform.
- * - Windows: Use Microsoft Edge (always installed on Windows 10+)
- * - macOS/Linux: Use Chrome
- * 
- * @returns The browser channel name for Playwright
+ * Determines which browser to launch.
+ *
+ * - MSTEAMS_MCP_BROWSER=chromium (default): the system Chrome (macOS/Linux)
+ *   or Edge (Windows) channel, exactly as before.
+ * - MSTEAMS_MCP_BROWSER=firefox: Playwright's Firefox build. Playwright
+ *   drives Firefox over the juggler protocol, which only Playwright's own
+ *   patched Firefox build implements (install it with
+ *   `npx playwright install firefox`); a stock Mozilla Firefox will not work.
+ * - MSTEAMS_MCP_BROWSER_EXECUTABLE: optional path to the browser executable.
+ *   For Chromium this overrides the channel.
+ *
+ * @throws Error if MSTEAMS_MCP_BROWSER is set to an unsupported value
  */
-function getBrowserChannel(): 'msedge' | 'chrome' {
-  return process.platform === 'win32' ? 'msedge' : 'chrome';
+export function getBrowserSettings(
+  env: NodeJS.ProcessEnv = process.env,
+  platform: NodeJS.Platform = process.platform,
+): BrowserSettings {
+  const requested = env.MSTEAMS_MCP_BROWSER?.trim().toLowerCase() || 'chromium';
+  if (requested !== 'chromium' && requested !== 'firefox') {
+    throw new Error(
+      `Unsupported MSTEAMS_MCP_BROWSER "${env.MSTEAMS_MCP_BROWSER}". Use "chromium" or "firefox".`
+    );
+  }
+
+  const executablePath = env.MSTEAMS_MCP_BROWSER_EXECUTABLE?.trim() || undefined;
+
+  if (requested === 'firefox') {
+    return { engine: 'firefox', executablePath };
+  }
+
+  // Windows: Microsoft Edge (always installed on Windows 10+); otherwise Chrome.
+  return {
+    engine: 'chromium',
+    channel: platform === 'win32' ? 'msedge' : 'chrome',
+    executablePath,
+  };
 }
 
 /**
  * Creates a browser context using a persistent profile.
  *
- * Uses the system's installed Chrome or Edge browser rather than downloading
- * Playwright's bundled Chromium (~180MB savings).
+ * By default uses the system's installed Chrome or Edge browser rather than
+ * downloading Playwright's bundled Chromium (~180MB savings). Set
+ * MSTEAMS_MCP_BROWSER=firefox to use Playwright's Firefox instead (see
+ * getBrowserSettings).
  *
  * The persistent profile at ~/.teams-mcp-server/browser-profile/ is shared
  * between headless and visible modes. This provides:
@@ -149,15 +191,19 @@ export async function createBrowserContext(
 ): Promise<BrowserManager> {
   ensureConfigDir();
 
-  const channel = getBrowserChannel();
+  const settings = getBrowserSettings();
 
   // Clean up any stale lock files before attempting to launch
   cleanupStaleSingletonLock();
 
   const launchBrowser = async (): Promise<BrowserManager> => {
-    const context = await chromium.launchPersistentContext(BROWSER_PROFILE_DIR, {
+    const browserType: BrowserType = settings.engine === 'firefox' ? firefox : chromium;
+    const context = await browserType.launchPersistentContext(BROWSER_PROFILE_DIR, {
       headless,
-      channel,
+      // An explicit executable replaces the Chromium channel.
+      ...(settings.executablePath
+        ? { executablePath: settings.executablePath }
+        : settings.channel ? { channel: settings.channel } : {}),
       viewport: { width: 1280, height: 800 },
       acceptDownloads: false,
     });
@@ -189,10 +235,18 @@ export async function createBrowserContext(
       }
     }
 
-    const browserName = channel === 'msedge' ? 'Microsoft Edge' : 'Google Chrome';
-    const installHint = channel === 'msedge'
-      ? 'Edge should be pre-installed on Windows. Try updating Windows or reinstalling Edge.'
-      : 'Install Chrome from https://www.google.com/chrome/ or run: npx playwright install chromium';
+    let browserName: string;
+    let installHint: string;
+    if (settings.engine === 'firefox') {
+      browserName = 'Firefox';
+      installHint = 'Run: npx playwright install firefox (Playwright only drives its own patched Firefox build, not stock Mozilla Firefox).';
+    } else if (settings.channel === 'msedge') {
+      browserName = 'Microsoft Edge';
+      installHint = 'Edge should be pre-installed on Windows. Try updating Windows or reinstalling Edge.';
+    } else {
+      browserName = 'Google Chrome';
+      installHint = 'Install Chrome from https://www.google.com/chrome/ or run: npx playwright install chromium';
+    }
 
     throw new Error(
       `Could not launch ${browserName}. ${installHint}\n\n` +
```

### Test plan

Run on the branch tip (clean tree), from a fresh `npm ci`:

```
npm ci --ignore-scripts && npm run build && npx vitest run && npx eslint src/
```

Results: `npm ci` exit 0; `npm run build` (tsc) exit 0; vitest 27 test files / 562 tests, all passed; `npx eslint src/` exit 0, no findings. Baseline at v0.30.2 on the same machine: 26 files / 557 tests passed, eslint clean.

New tests: `src/browser/context.test.ts` (5 tests: default channel on linux, Edge on win32, Firefox selection, executable pass-through, rejection of unknown values). Not tested: an actual Firefox launch.

### Compatibility / risk

Default is unchanged (Chromium + same channel selection), so existing users see no difference. The Chromium singleton-lock recovery path is untouched; Firefox does not use that lock file, so it simply never triggers. Cookie stores of Chromium and Firefox are not interchangeable, so switching engine on an existing profile directory means signing in again (the session file itself is engine independent).

---

## 2. Configurable login wait (`MSTEAMS_LOGIN_TIMEOUT_MS`)

**Suggested PR title:** `feat(login): make the sign-in wait configurable via MSTEAMS_LOGIN_TIMEOUT_MS`

### Problem

Two parts were originally carried downstream:

1. *Skip the headless attempt for `forceNew`.* In 0.27.2 `teams_login` with `forceNew` cleared cookies and then still tried a headless SSO refresh that depends on exactly those cookies, so it always failed, but only after consuming part of the single shared timeout, leaving less time for the human sign-in. **This is already fixed upstream**: since commit be8e2ef (#51, which includes #52 "forceNew deletes all previous login data and starts fresh"), `handleLogin` for `forceNew` wipes local data and calls `loginInVisibleBrowser()` directly; no headless attempt is made (`src/tools/auth-tools.ts`, `if (input.forceNew) { ... return loginInVisibleBrowser(); }`). Not proposed again.
2. *Make the timeout configurable.* Still open. The CLI gives the whole `teams_login` call a hard-coded 5 minute timeout (`LOGIN_TIMEOUT_MS` in `src/cli.ts`) and the browser-side wait for a human is a separate hard-coded 5 minutes (`MANUAL_LOGIN_TIMEOUT_MS` in `src/browser/auth.ts`). A sign-in with a password manager, MFA prompt and a "stay signed in" step, on a slow or cold-loading Teams, can exceed this, and the call fails with a timeout error (MCP -32001) even though the user is part-way through.

### Change

- New `src/utils/login-timeout.ts` with `getLoginTimeoutMs()`: reads `MSTEAMS_LOGIN_TIMEOUT_MS` (milliseconds); missing, non-numeric or non-positive values fall back to the existing default of 5 minutes. **The default is deliberately unchanged** (an earlier downstream variant raised it to 10 minutes; that is a policy choice left to the maintainer).
- Used for both the CLI call timeout and the in-browser manual-login wait, so raising it raises both (raising only one would be pointless).
- README example.

### Diff

```diff
diff --git a/README.md b/README.md
index 5f9402c..595d196 100644
--- a/README.md
+++ b/README.md
@@ -287,6 +287,8 @@ msteams status
 # Log in (opens a browser; tries silent SSO first)
 msteams login
 msteams login --force        # clear session and re-login
+# Slow SSO/MFA? Allow longer than the default 5 minutes (milliseconds):
+MSTEAMS_LOGIN_TIMEOUT_MS=600000 msteams login
 
 # Search messages
 msteams search "meeting notes"
diff --git a/src/browser/auth.ts b/src/browser/auth.ts
index a07e377..03f524b 100644
--- a/src/browser/auth.ts
+++ b/src/browser/auth.ts
@@ -12,6 +12,7 @@ import {
 } from '../constants.js';
 import { extractSubstrateToken } from '../auth/token-extractor.js';
 import * as logger from '../utils/logger.js';
+import { getLoginTimeoutMs } from '../utils/login-timeout.js';
 
 /**
  * Default Teams URL for initial login.
@@ -286,7 +287,7 @@ async function navigateToTeams(page: Page): Promise<AuthStatus> {
 }
 
 /** Maximum time to wait for the user to complete manual login (ms). */
-const MANUAL_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
+const MANUAL_LOGIN_TIMEOUT_MS = getLoginTimeoutMs();
 
 /**
  * Waits for the user to complete manual authentication.
diff --git a/src/cli.ts b/src/cli.ts
index 081a854..c99a19e 100644
--- a/src/cli.ts
+++ b/src/cli.ts
@@ -27,13 +27,11 @@ import { toolCategories } from './tools/registry.js';
 import { UsageError, parseToolArgs, resolveToolName, shortName } from './cli/resolve.js';
 import { printFailure, printResponse } from './cli/render.js';
 import { renderIndex, renderToolHelp } from './cli/help.js';
+import { getLoginTimeoutMs } from './utils/login-timeout.js';
 
 const require = createRequire(import.meta.url);
 const pkg = require('../package.json') as { version: string };
 
-/** Login can wait on SSO/MFA in a browser; everything else uses the default. */
-const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
-
 async function connect(): Promise<{ client: Client; cleanup: () => Promise<void> }> {
   const server = await createServer();
   const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
@@ -102,7 +100,7 @@ async function run(argv: string[]): Promise<number> {
       return 0;
     }
 
-    const timeout = name === 'teams_login' ? LOGIN_TIMEOUT_MS : undefined;
+    const timeout = name === 'teams_login' ? getLoginTimeoutMs() : undefined;
     const result = await client.callTool({ name, arguments: args }, undefined, { timeout });
     const payload = toolPayload(result);
     if (!payload) return result.isError ? 1 : 0;
diff --git a/src/utils/login-timeout.test.ts b/src/utils/login-timeout.test.ts
new file mode 100644
index 0000000..696ecc4
--- /dev/null
+++ b/src/utils/login-timeout.test.ts
@@ -0,0 +1,17 @@
+import { describe, it, expect } from 'vitest';
+import { DEFAULT_LOGIN_TIMEOUT_MS, getLoginTimeoutMs } from './login-timeout.js';
+
+describe('getLoginTimeoutMs', () => {
+  it('defaults to five minutes', () => {
+    expect(getLoginTimeoutMs({})).toBe(DEFAULT_LOGIN_TIMEOUT_MS);
+    expect(DEFAULT_LOGIN_TIMEOUT_MS).toBe(300_000);
+  });
+
+  it('honours MSTEAMS_LOGIN_TIMEOUT_MS', () => {
+    expect(getLoginTimeoutMs({ MSTEAMS_LOGIN_TIMEOUT_MS: '600000' })).toBe(600_000);
+  });
+
+  it.each(['', 'abc', '0', '-5', 'Infinity'])('ignores invalid value %j', (raw) => {
+    expect(getLoginTimeoutMs({ MSTEAMS_LOGIN_TIMEOUT_MS: raw })).toBe(DEFAULT_LOGIN_TIMEOUT_MS);
+  });
+});
diff --git a/src/utils/login-timeout.ts b/src/utils/login-timeout.ts
new file mode 100644
index 0000000..213853c
--- /dev/null
+++ b/src/utils/login-timeout.ts
@@ -0,0 +1,14 @@
+/** Login can wait on SSO/MFA in a browser; everything else uses the default timeout. */
+export const DEFAULT_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
+
+/**
+ * How long a `teams_login` may wait for a human to sign in: used both as the
+ * CLI's timeout for the whole call and as the browser's manual-login wait.
+ *
+ * Override with MSTEAMS_LOGIN_TIMEOUT_MS (milliseconds) for slow SSO/MFA
+ * flows. Missing, invalid or non-positive values fall back to the default.
+ */
+export function getLoginTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
+  const value = Number(env.MSTEAMS_LOGIN_TIMEOUT_MS);
+  return Number.isFinite(value) && value > 0 ? value : DEFAULT_LOGIN_TIMEOUT_MS;
+}
```

### Test plan

Run on the branch tip (clean tree), from a fresh `npm ci`:

```
npm ci --ignore-scripts && npm run build && npx vitest run && npx eslint src/
```

Results: `npm ci` exit 0; `npm run build` (tsc) exit 0; vitest 27 test files / 564 tests, all passed; `npx eslint src/` exit 0, no findings. Baseline at v0.30.2 on the same machine: 26 files / 557 tests passed, eslint clean.

New tests: `src/utils/login-timeout.test.ts` (7 tests: default, override, five invalid values).

### Compatibility / risk

No change unless the variable is set. The value is read at module load for the in-browser wait, i.e. once per process. The MCP server path (non-CLI) uses the SDK's own request timeout for tool calls, which this does not alter; only the CLI call timeout and the browser wait honour the variable.

---

## 3. HTTP token refresh when every access token has expired

**Suggested PR title:** not needed (already fixed upstream)

### Problem (as originally observed)

A session left idle past the access-token lifetime answered every tool with "Teams session has expired and automatic re-authentication failed", although its MSAL refresh token was still valid and a manual HTTP refresh renewed all tokens. In 0.27.2, `refreshTokensViaBrowser()` called `extractSubstrateToken()` first to record the old expiry; that skips expired tokens, so with all tokens expired it returned "No token found" before the HTTP refresh was ever attempted.

### Status at v0.30.2: not needed

Fixed by commit 194f9f0 "fix(auth): refresh after access token expiry (#28)" (first released in v0.28.0). In v0.30.2 `refreshTokensViaBrowser()` delegates to `refreshCoreTokens()` (`src/auth/token-refresh.ts`), which calls `refreshTokensViaHttp()` unconditionally, with the comment "An expired access token does not block this: the MSAL refresh token in session state is sufficient". The original early return on a missing token is gone. A regression test exists: `uses HTTP refresh after the cached access token has expired` in `src/auth/token-refresh.test.ts`, which passes in the baseline run (26 files / 557 tests passed).

No branch was created.

---

## 4. Send the browser `Origin` header only for the Teams web SPA client

**Suggested PR title:** `fix(auth): send Origin on HTTP token refresh only for the Teams web SPA client`

### Problem

`refreshAccessToken()` in `src/auth/token-refresh-http.ts` always sends `Origin: https://teams.microsoft.com`. That header is required when redeeming a refresh token for the Teams web client (a Single-Page Application registration, client id `5e3ce6c0-2b1f-4285-8d4b-75ee78787346`), but Microsoft Entra rejects cross-origin redemption for any other (native/public) client. A session whose MSAL cache was produced by a different public client, for example one obtained with the OAuth device-code flow, which cannot be used with the web client because Entra demands a client secret for it (AADSTS7000218), therefore could not be refreshed by this server: every refresh request carried an Origin header the client was not allowed to use. Per Entra's behaviour (rejecting cross-origin redemption for non-SPA clients), such refreshes fail while the header is sent; a downstream build that omits the header for these clients refreshes such sessions successfully.

This does not affect sessions created by this project's own browser login (those use the web client id, so behaviour is identical).

### Change

Add a `TEAMS_WEB_SPA_CLIENT_ID` constant and spread the `Origin` header into the request only when `clientId` equals it. Two tests assert the header is present for the SPA client id and absent for another id.

### Diff

```diff
diff --git a/src/auth/token-refresh-http.test.ts b/src/auth/token-refresh-http.test.ts
index 0e0ef53..2ecdca6 100644
--- a/src/auth/token-refresh-http.test.ts
+++ b/src/auth/token-refresh-http.test.ts
@@ -231,6 +231,43 @@ describe('refreshTokensViaHttp', () => {
     expect(vi.mocked(fetch)).toHaveBeenCalledTimes(4);
   });
 
+  describe('Origin header', () => {
+    /** Refreshes a session whose MSAL entries carry the given client ID; returns the token-endpoint request headers. */
+    async function tokenEndpointHeaders(clientId: string): Promise<Array<Record<string, string>>> {
+      const state = JSON.parse(
+        JSON.stringify(makeMockSessionState()).split(MOCK_CLIENT_ID).join(clientId),
+      ) as SessionState;
+      vi.mocked(readSessionState).mockReturnValue(state);
+      vi.mocked(getTeamsOrigin).mockReturnValue(state.origins[0]);
+
+      vi.mocked(fetch).mockImplementation(async (url: RequestInfo | URL) => {
+        if (String(url).includes('login.microsoftonline.com')) {
+          return new Response(JSON.stringify(makeTokenResponse('mocked-scope')), { status: 200 });
+        }
+        return new Response(JSON.stringify({ tokens: { skypeToken: 'new-skype-token', expiresIn: 86400 } }), { status: 200 });
+      });
+
+      const result = await refreshTokensViaHttp();
+      expect(result.ok).toBe(true);
+
+      return vi.mocked(fetch).mock.calls
+        .filter(([url]) => String(url).includes('login.microsoftonline.com'))
+        .map(([, init]) => init?.headers as Record<string, string>);
+    }
+
+    it('sends Origin for the Teams web SPA client', async () => {
+      const headers = await tokenEndpointHeaders(MOCK_CLIENT_ID);
+      expect(headers.length).toBeGreaterThan(0);
+      for (const h of headers) expect(h.Origin).toBe('https://teams.microsoft.com');
+    });
+
+    it('omits Origin for any other client ID', async () => {
+      const headers = await tokenEndpointHeaders('1fec8e78-bce4-4aaf-ab1b-5451cc387264');
+      expect(headers.length).toBeGreaterThan(0);
+      for (const h of headers) expect(h).not.toHaveProperty('Origin');
+    });
+  });
+
   it('updates skypetoken_asm cookies in session state', async () => {
     const state = makeMockSessionState();
     vi.mocked(readSessionState).mockReturnValue(state);
diff --git a/src/auth/token-refresh-http.ts b/src/auth/token-refresh-http.ts
index 7fd3eb5..217b8dc 100644
--- a/src/auth/token-refresh-http.ts
+++ b/src/auth/token-refresh-http.ts
@@ -106,6 +106,14 @@ interface AuthsvcResponse {
 /** Azure AD OAuth2 token endpoint template. */
 const TOKEN_ENDPOINT = 'https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token';
 
+/**
+ * Client ID of the Teams web app, which is registered as a Single-Page
+ * Application. Only SPA clients redeem refresh tokens cross-origin; sending an
+ * Origin header for any other (e.g. native/public) client makes Azure AD
+ * reject the request.
+ */
+const TEAMS_WEB_SPA_CLIENT_ID = '5e3ce6c0-2b1f-4285-8d4b-75ee78787346';
+
 /** Teams authsvc endpoint for skype token exchange. */
 const AUTHSVC_ENDPOINT = 'https://authsvc.teams.microsoft.com/v1.0/authz';
 
@@ -244,12 +252,14 @@ async function refreshAccessToken(
     // The Origin header is required because the Teams client ID is registered as
     // a Single-Page Application (SPA). Azure AD validates that refresh token grants
     // from SPA clients include a cross-origin Origin header matching a registered
-    // redirect URI. Without this, Azure AD returns AADSTS9002327.
+    // redirect URI. Without this, Azure AD returns AADSTS9002327. Other
+    // (non-SPA) clients must NOT send it: Azure AD rejects cross-origin
+    // redemption for them.
     const response = await fetch(url, {
       method: 'POST',
       headers: {
         'Content-Type': 'application/x-www-form-urlencoded',
-        'Origin': 'https://teams.microsoft.com',
+        ...(clientId === TEAMS_WEB_SPA_CLIENT_ID ? { 'Origin': 'https://teams.microsoft.com' } : {}),
       },
       body: body.toString(),
       signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS),
```

### Test plan

Run on the branch tip (clean tree), from a fresh `npm ci`:

```
npm ci --ignore-scripts && npm run build && npx vitest run && npx eslint src/
```

Results: `npm ci` exit 0; `npm run build` (tsc) exit 0; vitest 26 test files / 559 tests, all passed; `npx eslint src/` exit 0, no findings. Baseline at v0.30.2 on the same machine: 26 files / 557 tests passed, eslint clean.

New tests: two cases under `Origin header` in `src/auth/token-refresh-http.test.ts` (header present for the web client id; absent for another client id). The existing refresh tests continue to pass unchanged.

### Compatibility / risk

Identical behaviour for the Teams web client id, which is the only client this project's login produces. For other client ids the request is now sent without `Origin`, which is what Entra requires for non-SPA clients. If Microsoft ever renames or rotates the web client id this branch would silently stop sending the header, so the constant should be kept in step with the id the Teams web app uses; maintainers may prefer a different gate (for example a flag derived from the cache entry) and the diff is small enough to adapt.

---

## 5. Obtain the Substrate token on sign-in; accept all Teams hosts for messaging cookies

**Suggested PR title:** `fix(auth): prime the Substrate token during login and accept every Teams host for messaging cookies`

### Problem

Observed: after a person signed in in the visible Teams window, `teams_login` reported success, yet every search/messaging tool then failed with AUTH_REQUIRED and the log said "No Substrate token found in session - cannot refresh, browser login required". Three independent causes, checked against v0.30.2:

1. **Lazy Substrate token (still present).** Teams web acquires its SubstrateSearch token only when search is first used, so a signed-in tab can go indefinitely without one. v0.30.2 only polls for it (`waitForTokenRefresh`) and, since #51, keeps the visible login open until a live token appears (`ensureAuthenticated` -> `waitForManualLogin`), but nothing ever causes Teams to request it. So the visible window can sit until the 5 minute timeout. #51 turned the earlier "save a session with no usable token" outcome into "wait for a token that may never come"; priming resolves that wait.
2. **Teams hosts (partly fixed upstream).** The login-page and authenticated-page detection was legacy-host only in 0.27.2; #47 (10c0faf, `TEAMS_HOSTS`) fixed that, so those parts are not re-proposed. Still legacy-only in v0.30.2: `extractMessageAuth()` and `getMessageAuthStatus()` in `src/auth/token-extractor.ts` only accept cookies whose domain contains `teams.microsoft.com`. Observed: with the session on `teams.cloud.microsoft`, `skypetoken_asm` was on `.asyncgw.teams.microsoft.com` but the `authtoken` cookie was set for `teams.cloud.microsoft`, so every chat/unread tool failed with AUTH_REQUIRED while all other tokens were present. The HTTP refresh (`updateSkypeCookies`) additionally writes the refreshed `authtoken` under `teams.microsoft.com` only, leaving a stale copy on the other host, so "first match" can also return an expired token.
3. **Login start URL (partly still relevant).** `navigateToTeams` always opens `https://teams.microsoft.com`. In the 0.27.2 observation the saved session held only that origin and no MSAL access tokens while the account's Teams (and its MSAL cache) lived on `teams.cloud.microsoft`. Whether Microsoft's redirect now covers this at v0.30.2 was not re-verified live; an override is provided as a low-risk escape hatch, default unchanged.

### Change

- `src/browser/auth.ts`: new exported `primeSubstrateToken(page)` focuses the search box (selector list matching the several UI generations), types one character, waits, and presses Escape; returns false (never throws) if there is no search box. Called (a) from `waitForTokenRefresh` when no live token is present, and (b) from `waitForManualLogin` when the page is on a Teams host, not on the login page and still without a live token, at most 3 times (stops after the first success), re-checking immediately afterwards. This rebases the idea onto #51's wait-for-live-token flow instead of the old "save and return" branch. The 20 s token poll is unchanged.
- `src/auth/token-extractor.ts`: messaging cookies (`skypetoken_asm`, `authtoken`) are accepted from any host in the existing `TEAMS_HOSTS` list, and when several exist the one with the latest JWT expiry wins. `getMessageAuthStatus()` uses the same helper.
- `MSTEAMS_TEAMS_URL` environment variable (default `https://teams.microsoft.com`, unchanged) selects the login start URL; documented in the README.
- Tests for priming, the priming call inside `ensureAuthenticated`, and the cookie handling.

### Diff

```diff
diff --git a/README.md b/README.md
index 5f9402c..72681cf 100644
--- a/README.md
+++ b/README.md
@@ -86,6 +86,8 @@ Then configure your MCP client:
 
 The server uses your system's Chrome (macOS/Linux) or Edge (Windows) for authentication.
 
+Login opens `https://teams.microsoft.com` by default. Set `MSTEAMS_TEAMS_URL` (for example `https://teams.cloud.microsoft`) to start on another Teams host. While waiting for sign-in, the server may focus the Teams search box and type one character, because Teams only requests its search token the first time search is used.
+
 ### CLI
 
 The same functionality is also available as a standalone command-line tool, `msteams`. This is useful when you want Teams from a shell or script instead of an MCP client.
diff --git a/src/auth/token-extractor.test.ts b/src/auth/token-extractor.test.ts
new file mode 100644
index 0000000..89d9917
--- /dev/null
+++ b/src/auth/token-extractor.test.ts
@@ -0,0 +1,73 @@
+import { describe, it, expect, vi } from 'vitest';
+
+vi.mock('./session-store.js', () => ({
+  readSessionState: vi.fn(),
+  writeSessionState: vi.fn(),
+  readTokenCache: vi.fn(),
+  writeTokenCache: vi.fn(),
+  getTeamsOrigin: vi.fn(),
+}));
+
+import { extractMessageAuth, getMessageAuthStatus } from './token-extractor.js';
+import { readSessionState, type SessionState } from './session-store.js';
+
+function jwt(claims: Record<string, unknown>): string {
+  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
+  return `${b64({ alg: 'none' })}.${b64(claims)}.sig`;
+}
+
+const inSeconds = (s: number) => Math.floor(Date.now() / 1000) + s;
+
+function cookie(name: string, value: string, domain: string) {
+  return { name, value, domain, path: '/', expires: -1, httpOnly: false, secure: true, sameSite: 'None' as const };
+}
+
+function sessionWith(cookies: ReturnType<typeof cookie>[]): SessionState {
+  return { cookies, origins: [] };
+}
+
+describe('messaging cookies across Teams hosts', () => {
+  const skype = jwt({ skypeid: 'orgid:user-guid', exp: inSeconds(3600) });
+  const auth = (exp: number) => jwt({ oid: 'user-guid', exp });
+
+  it('finds the authtoken cookie on teams.cloud.microsoft', () => {
+    const state = sessionWith([
+      cookie('skypetoken_asm', skype, '.asyncgw.teams.microsoft.com'),
+      cookie('authtoken', `Bearer%3D${auth(inSeconds(3600))}`, 'teams.cloud.microsoft'),
+    ]);
+
+    const result = extractMessageAuth(state);
+
+    expect(result?.skypeToken).toBe(skype);
+    expect(result?.userMri).toBe('8:orgid:user-guid');
+  });
+
+  it('prefers the authtoken with the latest expiry when several hosts hold one', () => {
+    const stale = auth(inSeconds(60));
+    const fresh = auth(inSeconds(3600));
+    const state = sessionWith([
+      cookie('skypetoken_asm', skype, '.asyncgw.teams.microsoft.com'),
+      cookie('authtoken', `Bearer%3D${fresh}`, 'teams.microsoft.com'),
+      cookie('authtoken', `Bearer%3D${stale}`, 'teams.cloud.microsoft'),
+    ]);
+
+    expect(extractMessageAuth(state)?.authToken).toBe(fresh);
+  });
+
+  it('ignores cookies from unrelated domains', () => {
+    const state = sessionWith([
+      cookie('skypetoken_asm', skype, '.asyncgw.teams.microsoft.com'),
+      cookie('authtoken', `Bearer%3D${auth(inSeconds(3600))}`, 'example.com'),
+    ]);
+
+    expect(extractMessageAuth(state)).toBeNull();
+  });
+
+  it('reports messaging status from a skypetoken on the new host', () => {
+    vi.mocked(readSessionState).mockReturnValue(
+      sessionWith([cookie('skypetoken_asm', skype, 'teams.cloud.microsoft')]),
+    );
+
+    expect(getMessageAuthStatus().hasToken).toBe(true);
+  });
+});
diff --git a/src/auth/token-extractor.ts b/src/auth/token-extractor.ts
index 2706d70..546693e 100644
--- a/src/auth/token-extractor.ts
+++ b/src/auth/token-extractor.ts
@@ -15,7 +15,7 @@ import {
   type SessionState,
 } from './session-store.js';
 import { parseJwtProfile, type UserProfile } from '../utils/parsers.js';
-import { MRI_TYPE_PREFIX, ORGID_PREFIX, MRI_ORGID_PREFIX, ASSIGNMENTS_APP_ID, GRAPH_AUDIENCES } from '../constants.js';
+import { MRI_TYPE_PREFIX, ORGID_PREFIX, MRI_ORGID_PREFIX, ASSIGNMENTS_APP_ID, GRAPH_AUDIENCES, TEAMS_HOSTS } from '../constants.js';
 
 // ============================================================================
 // JWT Utilities
@@ -341,6 +341,42 @@ export function extractRegionConfig(state?: SessionState): RegionConfig | null {
   return null;
 }
 
+type SessionCookie = SessionState['cookies'][number];
+
+/**
+ * Picks the named Teams cookie with the latest JWT expiry.
+ *
+ * Teams cookies can be set on several Teams hosts at once (e.g. the legacy
+ * `teams.microsoft.com` and the newer `teams.cloud.microsoft`), and the HTTP
+ * refresh rewrites only one of them, so the first match can be a stale copy.
+ * `decode` maps the raw cookie value to the JWT to inspect.
+ */
+function newestTeamsCookie(
+  cookies: SessionCookie[],
+  name: string,
+  decode: (raw: string) => string = raw => raw,
+): SessionCookie | undefined {
+  let best: SessionCookie | undefined;
+  let bestExpiry = -1;
+  for (const cookie of cookies) {
+    if (cookie.name !== name || !cookie.value) continue;
+    if (!TEAMS_HOSTS.some(host => cookie.domain?.includes(host))) continue;
+    const expiry = getJwtExpiry(decode(cookie.value))?.getTime() ?? 0;
+    if (expiry > bestExpiry) {
+      bestExpiry = expiry;
+      best = cookie;
+    }
+  }
+  return best;
+}
+
+/** Decodes an `authtoken` cookie value (URL-encoded, may carry a `Bearer=` prefix). */
+function decodeAuthTokenCookie(raw: string): string {
+  let value = raw;
+  try { value = decodeURIComponent(raw); } catch { /* keep raw */ }
+  return value.startsWith('Bearer=') ? value.substring(7) : value;
+}
+
 /**
  * Extracts authentication info needed for messaging API.
  * Unlike other APIs, messaging uses cookies rather than localStorage tokens.
@@ -350,19 +386,15 @@ export function extractMessageAuth(state?: SessionState): MessageAuthInfo | null
   if (!sessionState) return null;
 
   const cookies = sessionState.cookies ?? [];
-  const teamsCookies = cookies.filter(c => c.domain?.includes('teams.microsoft.com'));
 
-  // Extract the two required cookies
-  const skypeToken = teamsCookies.find(c => c.name === 'skypetoken_asm')?.value ?? null;
-  const rawAuthToken = teamsCookies.find(c => c.name === 'authtoken')?.value ?? null;
+  // Extract the two required cookies from any Teams host
+  const skypeToken = newestTeamsCookie(cookies, 'skypetoken_asm')?.value ?? null;
+  const rawAuthToken = newestTeamsCookie(cookies, 'authtoken', decodeAuthTokenCookie)?.value ?? null;
   
   if (!skypeToken || !rawAuthToken) return null;
 
   // Decode authtoken (URL-encoded, may have 'Bearer=' prefix)
-  let authToken = decodeURIComponent(rawAuthToken);
-  if (authToken.startsWith('Bearer=')) {
-    authToken = authToken.substring(7);
-  }
+  const authToken = decodeAuthTokenCookie(rawAuthToken);
 
   // Extract userMri from skypeToken's skypeid claim, or fall back to authToken's oid
   const userMri = extractMriFromSkypeToken(skypeToken) 
@@ -388,9 +420,7 @@ export function getMessageAuthStatus(): {
   }
 
   const cookies = sessionState.cookies ?? [];
-  const skypeToken = cookies.find(
-    c => c.domain?.includes('teams.microsoft.com') && c.name === 'skypetoken_asm'
-  )?.value;
+  const skypeToken = newestTeamsCookie(cookies, 'skypetoken_asm')?.value;
 
   if (!skypeToken) {
     return { hasToken: false };
diff --git a/src/browser/auth.test.ts b/src/browser/auth.test.ts
index 4d35084..11741cf 100644
--- a/src/browser/auth.test.ts
+++ b/src/browser/auth.test.ts
@@ -4,7 +4,7 @@ import type { Page } from 'playwright';
 vi.mock('./context.js', () => ({ saveSessionState: vi.fn() }));
 vi.mock('../auth/token-extractor.js', () => ({ extractSubstrateToken: vi.fn() }));
 
-import { ensureAuthenticated, getAuthStatus, isTeamsUrl } from './auth.js';
+import { ensureAuthenticated, getAuthStatus, isTeamsUrl, primeSubstrateToken } from './auth.js';
 
 /** A page on `url` with no Teams DOM markers and `tokenMinutes` left on MSAL. */
 function fakePage(url: string, tokenMinutes: number): Page {
@@ -57,6 +57,28 @@ describe('getAuthStatus', () => {
 });
 
 describe('ensureAuthenticated', () => {
+  it('primes the search box while waiting for a token that Teams has not requested', async () => {
+    const tokenMinutes = { value: -1 };
+    const typed: string[] = [];
+    const page = {
+      goto: async () => null,
+      waitForURL: async () => { throw new Error('timeout'); },
+      url: () => 'https://teams.cloud.microsoft/',
+      evaluate: async () => tokenMinutes.value,
+      // Yield to the event loop so a regression fails by test timeout, not by hanging.
+      waitForTimeout: () => new Promise<void>(resolve => setTimeout(resolve, 1)),
+      locator: () => ({ first: () => ({ waitFor: async () => {}, click: async () => {} }) }),
+      keyboard: {
+        type: async (text: string) => { typed.push(text); tokenMinutes.value = 55; },
+        press: async () => {},
+      },
+    } as unknown as Page;
+
+    await ensureAuthenticated(page, {} as never, () => {}, false, false);
+
+    expect(typed).toEqual(['a']);
+  });
+
   it('keeps a visible browser open for sign-in when Teams loads with expired tokens', async () => {
     // Teams can render without redirecting to login while the Microsoft SSO
     // session is gone; only a fresh MSAL token proves the user has signed in.
@@ -77,3 +99,37 @@ describe('ensureAuthenticated', () => {
     expect(tokenMinutes).toEqual([55]);
   });
 });
+
+describe('primeSubstrateToken', () => {
+  function primeablePage(visible: boolean) {
+    const calls: string[] = [];
+    const page = {
+      locator: () => ({
+        first: () => ({
+          waitFor: async () => { if (!visible) throw new Error('not visible'); },
+          click: async () => { calls.push('click'); },
+        }),
+      }),
+      keyboard: {
+        type: async (text: string) => { calls.push(`type:${text}`); },
+        press: async (key: string) => { calls.push(`press:${key}`); },
+      },
+      waitForTimeout: async () => {},
+    } as unknown as Page;
+    return { page, calls };
+  }
+
+  it('focuses the search box, types one character, then dismisses it', async () => {
+    const { page, calls } = primeablePage(true);
+
+    expect(await primeSubstrateToken(page)).toBe(true);
+    expect(calls).toEqual(['click', 'type:a', 'press:Escape']);
+  });
+
+  it('reports false, without throwing, when there is no search box', async () => {
+    const { page, calls } = primeablePage(false);
+
+    expect(await primeSubstrateToken(page)).toBe(false);
+    expect(calls).toEqual([]);
+  });
+});
diff --git a/src/browser/auth.ts b/src/browser/auth.ts
index a07e377..7346449 100644
--- a/src/browser/auth.ts
+++ b/src/browser/auth.ts
@@ -21,7 +21,16 @@ import * as logger from '../utils/logger.js';
  * to the appropriate URL (teams.microsoft.us, etc.) after authentication.
  * We then extract the correct base URL from DISCOVER-REGION-GTM for all API calls.
  */
-const TEAMS_URL = 'https://teams.microsoft.com';
+const DEFAULT_TEAMS_URL = 'https://teams.microsoft.com';
+
+/**
+ * Teams URL to open for login. Set MSTEAMS_TEAMS_URL (e.g.
+ * `https://teams.cloud.microsoft`) to start on a different Teams host; the
+ * default is unchanged.
+ */
+function getTeamsUrl(): string {
+  return process.env.MSTEAMS_TEAMS_URL?.trim() || DEFAULT_TEAMS_URL;
+}
 
 // ─────────────────────────────────────────────────────────────────────────────
 // Progress Overlay UI
@@ -244,7 +253,7 @@ export function isTeamsUrl(url: string): boolean {
  * silently failing with an invisible browser stuck on an unexpected page.
  */
 async function navigateToTeams(page: Page): Promise<AuthStatus> {
-  await page.goto(TEAMS_URL, { waitUntil: 'domcontentloaded' });
+  await page.goto(getTeamsUrl(), { waitUntil: 'domcontentloaded' });
 
   // Login redirect happens ~3-4 seconds after navigation when the session is
   // invalid (MSAL tries silent auth first, then redirects). No redirect within
@@ -285,6 +294,9 @@ async function navigateToTeams(page: Page): Promise<AuthStatus> {
   };
 }
 
+/** How many times a manual-login wait nudges Teams to request its search token. */
+const MAX_PRIME_ATTEMPTS = 3;
+
 /** Maximum time to wait for the user to complete manual login (ms). */
 const MANUAL_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
 
@@ -308,9 +320,21 @@ async function waitForManualLogin(
 
   log('Waiting for manual login...');
 
+  // Teams can sit signed in on a Teams host without ever requesting its
+  // Substrate token (it asks lazily, on first search). Prime it a few times at
+  // most, then rely on the user / MSAL.
+  let primeAttempts = 0;
+
   while (Date.now() - startTime < MANUAL_LOGIN_TIMEOUT_MS) {
     const status = await getAuthStatus(page);
 
+    if (!status.isAuthenticated && !status.isOnLoginPage
+        && isTeamsUrl(status.currentUrl) && primeAttempts < MAX_PRIME_ATTEMPTS) {
+      primeAttempts++;
+      if (await primeSubstrateToken(page)) primeAttempts = MAX_PRIME_ATTEMPTS;
+      continue; // re-check straight away: the token may now be present
+    }
+
     if (status.isAuthenticated) {
       log('Authentication successful!');
 
@@ -407,6 +431,37 @@ async function checkBrowserTokenExpiry(page: Page): Promise<number> {
   }
 }
 
+/** Selectors matching the Teams search box across UI versions. */
+const SEARCH_BOX_SELECTORS = [
+  '[data-tid="search-box"]',
+  'input[placeholder*="Search"]',
+  '#ms-searchux-input',
+];
+
+/**
+ * Nudges Teams web into acquiring its Substrate (search) token.
+ *
+ * Teams requests that token lazily, the first time search is used, so a
+ * signed-in tab can sit indefinitely without one. Focusing the search box and
+ * typing a single character makes it ask. Only worth calling on a Teams page
+ * that has no live token yet.
+ *
+ * @returns true if the search box was found and used
+ */
+export async function primeSubstrateToken(page: Page): Promise<boolean> {
+  try {
+    const box = page.locator(SEARCH_BOX_SELECTORS.join(', ')).first();
+    await box.waitFor({ state: 'visible', timeout: 15000 });
+    await box.click();
+    await page.keyboard.type('a');
+    await page.waitForTimeout(3000);
+    await page.keyboard.press('Escape');
+    return true;
+  } catch {
+    return false;
+  }
+}
+
 /**
  * Waits for MSAL to refresh tokens in the browser.
  * 
@@ -424,6 +479,12 @@ async function waitForTokenRefresh(
 ): Promise<boolean> {
   const log = onProgress ?? ((msg: string) => logger.debug('auth', msg));
   
+  if (await checkBrowserTokenExpiry(page) <= 0) {
+    log(await primeSubstrateToken(page)
+      ? 'Primed Teams search to acquire a Substrate token.'
+      : 'Could not find the Teams search box to prime.');
+  }
+
   log('Waiting for MSAL to refresh tokens...');
   const startTime = Date.now();
   
```

### Test plan

Run on the branch tip (clean tree), from a fresh `npm ci`:

```
npm ci --ignore-scripts && npm run build && npx vitest run && npx eslint src/
```

Results: `npm ci` exit 0; `npm run build` (tsc) exit 0; vitest 27 test files / 564 tests, all passed; `npx eslint src/` exit 0, no findings. Baseline at v0.30.2 on the same machine: 26 files / 557 tests passed, eslint clean.

New tests: `src/auth/token-extractor.test.ts` (4 tests) and 3 tests added to `src/browser/auth.test.ts` (two for `primeSubstrateToken`, one `ensureAuthenticated` flow that completes only after priming). As a negative check, with the v0.30.2 `token-extractor.ts` swapped back in, 2 of the 4 extractor tests fail (cookie on `teams.cloud.microsoft` not found, status not reported) and 2 pass (the "latest expiry wins" and "unrelated domain" cases, which a first-match legacy filter cannot get wrong with one candidate); the priming tests were not run against the old `auth.ts` because it lacks the export.

### Compatibility / risk

- Priming types one character into the Teams search box and presses Escape, only while a Teams page has no live search token, so it is invisible once a token exists. If Teams changes the search box markup, priming returns false and the behaviour falls back to today's.
- Cookie handling now also accepts the other hosts listed in `TEAMS_HOSTS` (including `teams.microsoft.us` and `dod.teams.microsoft.us`), consistent with #47's intent; legacy-host sessions resolve exactly as before. The authtoken cookie's JWT is decoded only to read `exp`; a cookie that does not decode counts as expiry 0 and loses to one that does.
- Downstream also raised the token wait from 20 s to 90 s for a slow container; that is environment-specific and is not proposed here.

---

## What cannot be upstreamed

The downstream deployment does not build from a current upstream release. It pins the older commit a69069cd (npm 0.27.2) and applies the five compiled-`dist/` patches (`patch-msteams-*.cjs`) at image build time. Those scripts match literal text in compiled JavaScript, so four of the five no longer apply to v0.30.x (only the Origin one still matches textually) and they cannot themselves be contributed: upstream's source of truth is TypeScript, and the fixes above are the source-level equivalents (or, for the refresh-expired fix and the `forceNew` headless skip, already upstream). Until these land in a release the deployment either has to keep pinning the old commit, or be rebased onto a newer upstream together with re-derived patches. Deployment-specific pieces (the 90 s token wait, a default login host, a 10 minute login default, and a device-code sign-in helper) are intentionally left out of the drafts.
