// Build-time patch for the msteams-mcp package (see ../Dockerfile and ../README.md).
// Makes teams_login capture a Substrate search token, on the newer Teams web domain as well as the
// legacy one.
//
// Observed: after a person signed in to the visible Teams window, `teams_login` reported "Login
// completed successfully. Session has been saved." and yet every messaging and search tool failed
// with AUTH_REQUIRED; the server log said "No Substrate token found in session - cannot refresh,
// browser login required". Causes, in upstream's dist/browser/auth.js and
// dist/auth/token-extractor.js:
//
//   - The login navigates to the legacy teams.microsoft.com and saves only that origin's storage,
//     while a signed-in Teams (and its MSAL cache) may live on teams.cloud.microsoft: the saved
//     session then held one origin and no MSAL access tokens. getAuthStatus() also only recognised
//     the legacy domain. Now the login URL defaults to teams.cloud.microsoft (override:
//     MSTEAMS_TEAMS_URL) and any Teams domain counts as a Teams page.
//   - Already-authenticated profile, VISIBLE browser: upstream saves whatever tokens happen to be
//     in localStorage and returns ("tokens may need refresh"); it never waits for a Substrate
//     token. HEADLESS: it waits 20 s for MSAL to produce one and gives up. Teams web acquires its
//     SubstrateSearch token lazily, when search is used, so a signed-in tab can go indefinitely
//     without one and neither branch does anything that makes it ask. Now the search box is primed
//     (focus plus one keystroke) before polling, the wait is longer (override:
//     MSTEAMS_TOKEN_REFRESH_WAIT_MS, default 90 s) and the visible branch waits the same way.
//   - Messaging (chatsvc) authenticates with two cookies and extractMessageAuth only looked for
//     them on teams.microsoft.com; on the newer domain the `authtoken` cookie is set for that
//     domain instead. Both domains are accepted and the NEWEST token by JWT expiry wins, because
//     the hourly HTTP refresh writes the refreshed cookie under the legacy domain and leaves the
//     other copy to go stale.

const fs = require("node:fs");
const path = require("node:path");

const pkgDir = process.argv[2];
if (!pkgDir) {
  throw new Error(
    "Usage: node patch-msteams-substrate-token.cjs <path to installed msteams-mcp package>",
  );
}

let filePath;
let source;
function replaceOnce(needle, replacement, what) {
  const count = source.split(needle).length - 1;
  if (count !== 1) {
    throw new Error(
      `msteams-mcp: expected exactly one ${what} in ${path.relative(pkgDir, filePath)}, found ${count} -- ` +
        "upstream reshaped it; re-verify before patching",
    );
  }
  source = source.replace(needle, replacement);
}
function patchFile(rel, marker, apply, verifyNeedles) {
  filePath = path.join(pkgDir, rel);
  source = fs.readFileSync(filePath, "utf8");
  if (source.includes(marker)) return; // already applied
  apply();
  fs.writeFileSync(filePath, source, "utf8");
  const verify = fs.readFileSync(filePath, "utf8");
  for (const needle of [marker, ...verifyNeedles]) {
    if (!verify.includes(needle)) {
      throw new Error(`msteams-mcp: patch did not land in ${rel} (${needle})`);
    }
  }
}

const marker = "async function primeSubstrateToken(page)";
patchFile("dist/browser/auth.js", marker, () => {

// The login navigates to the legacy teams.microsoft.com and saves only that
// origin's storage: the saved session held one origin, teams.microsoft.com,
// and zero MSAL access tokens, while a signed-in Teams (and its MSAL cache) can
// live on teams.cloud.microsoft.
// session-store.js already prefers that origin when READING a session; make
// the login go there too, and accept it as an authenticated Teams page
// (getAuthStatus only recognised teams.microsoft.com).
replaceOnce(
  "const TEAMS_URL = 'https://teams.microsoft.com';",
  "const TEAMS_URL = process.env.MSTEAMS_TEAMS_URL || 'https://teams.cloud.microsoft'; // patched: was teams.microsoft.com",
  "TEAMS_URL",
);
replaceOnce(
  "    // If on Teams domain, check for authenticated content\n    if (currentUrl.includes('teams.microsoft.com')) {",
  "    // If on Teams domain, check for authenticated content (patched: any Teams domain)\n    if (isTeamsUrl(currentUrl)) {",
  "getAuthStatus() Teams-domain check",
);

replaceOnce(
  "const TOKEN_REFRESH_WAIT_TIMEOUT_MS = 20000;",
  "const TOKEN_REFRESH_WAIT_TIMEOUT_MS = Number(process.env.MSTEAMS_TOKEN_REFRESH_WAIT_MS) || 90000; // patched: was 20000",
  "TOKEN_REFRESH_WAIT_TIMEOUT_MS",
);

replaceOnce(
  "async function waitForTokenRefresh(page, context, onProgress) {\n" +
    "    const log = onProgress ?? ((msg) => logger.debug('auth', msg));\n" +
    "    log('Waiting for MSAL to refresh tokens...');\n",
  "// patched: Teams web only acquires its SubstrateSearch token when search is\n" +
    "// used, so poke the search box before polling for it.\n" +
    marker + " {\n" +
    "    const selectors = ['[data-tid=\"search-box\"]', 'input[placeholder*=\"Search\"]', '#ms-searchux-input'];\n" +
    "    for (const selector of selectors) {\n" +
    "        try {\n" +
    "            const box = page.locator(selector).first();\n" +
    "            await box.waitFor({ state: 'visible', timeout: 15000 });\n" +
    "            await box.click();\n" +
    "            await page.keyboard.type('a');\n" +
    "            await page.waitForTimeout(3000);\n" +
    "            await page.keyboard.press('Escape');\n" +
    "            return true;\n" +
    "        }\n" +
    "        catch {\n" +
    "            continue;\n" +
    "        }\n" +
    "    }\n" +
    "    return false;\n" +
    "}\n" +
    "async function waitForTokenRefresh(page, context, onProgress) {\n" +
    "    const log = onProgress ?? ((msg) => logger.debug('auth', msg));\n" +
    "    if ((await checkBrowserTokenExpiry(page)) <= 0) {\n" +
    "        log((await primeSubstrateToken(page)) ? 'Primed Teams search to acquire a Substrate token.' : 'Could not find the Teams search box to prime.');\n" +
    "    }\n" +
    "    log('Waiting for MSAL to refresh tokens...');\n",
  "waitForTokenRefresh() header",
);

replaceOnce(
  "        // In visible mode, the tokens might refresh while user interacts, or they can\n" +
    "        // manually complete any prompts. We've saved what we have.\n" +
    "        log('Session state saved (tokens may need refresh).');\n" +
    "        return;\n",
  "        // patched: wait for (and prime) the Substrate token in visible mode too,\n" +
    "        // instead of saving a session no messaging/search call can use.\n" +
    "        const refreshed = await waitForTokenRefresh(page, context, onProgress);\n" +
    "        log(refreshed ? 'Session state saved.' : 'Session state saved (no Substrate token acquired yet).');\n" +
    "        return;\n",
  "visible-mode 'tokens may need refresh' branch",
);

}, ["MSTEAMS_TOKEN_REFRESH_WAIT_MS", "MSTEAMS_TEAMS_URL", "if (isTeamsUrl(currentUrl)) {", "Session state saved (no Substrate token acquired yet)."]);

// Messaging (chatsvc) authenticates with two cookies, and extractMessageAuth
// only looked for them on teams.microsoft.com. On teams.cloud.microsoft the
// `authtoken` cookie is set for that domain instead (observed:
// skypetoken_asm on .asyncgw.teams.microsoft.com, authtoken on
// teams.cloud.microsoft), so every chat/unread tool failed AUTH_REQUIRED with
// all other tokens present. Accept both domains -- and pick the NEWEST token
// by JWT expiry, because the hourly HTTP refresh (token-refresh-http.js
// updateAuthTokenCookie) writes the refreshed authtoken under
// teams.microsoft.com and leaves the teams.cloud.microsoft copy to go stale.
patchFile("dist/auth/token-extractor.js", "newestTeamsCookie(", () => {
  replaceOnce(
    "    const teamsCookies = cookies.filter(c => c.domain?.includes('teams.microsoft.com'));\n" +
      "    // Extract the two required cookies\n" +
      "    const skypeToken = teamsCookies.find(c => c.name === 'skypetoken_asm')?.value ?? null;\n" +
      "    const rawAuthToken = teamsCookies.find(c => c.name === 'authtoken')?.value ?? null;\n",
    "    // patched: both Teams cookie domains, newest token wins (see patch-msteams-substrate-token.cjs)\n" +
      "    const teamsCookies = cookies.filter(c => c.domain?.includes('teams.microsoft.com') || c.domain?.includes('teams.cloud.microsoft'));\n" +
      "    const newestTeamsCookie = (name, bearer) => {\n" +
      "        let best = null, bestExp = -1;\n" +
      "        for (const c of teamsCookies) {\n" +
      "            if (c.name !== name || !c.value) continue;\n" +
      "            let v = c.value;\n" +
      "            if (bearer) { try { v = decodeURIComponent(v); } catch { } if (v.startsWith('Bearer=')) v = v.substring(7); }\n" +
      "            const exp = getJwtExpiry(v)?.getTime() ?? 0;\n" +
      "            if (exp > bestExp) { bestExp = exp; best = c.value; }\n" +
      "        }\n" +
      "        return best;\n" +
      "    };\n" +
      "    const skypeToken = newestTeamsCookie('skypetoken_asm', false);\n" +
      "    const rawAuthToken = newestTeamsCookie('authtoken', true);\n",
    "extractMessageAuth() cookie lookup",
  );
}, ["teams.cloud.microsoft"]);
