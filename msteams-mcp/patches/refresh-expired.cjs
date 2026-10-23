// Build-time patch for the msteams-mcp package (see ../Dockerfile and ../README.md).
// Lets the automatic token renewal recover a session whose access tokens have all expired, which is
// what every session looks like after an idle hour.
//
// Observed: a session left idle overnight answered every tool with "Teams session has expired and
// automatic re-authentication failed", although its refresh token was still valid (calling
// refreshTokensViaHttp() by hand renewed all three tokens at once). dist/auth/token-refresh.js
// refreshTokensViaBrowser() first calls extractSubstrateToken() to record the old expiry, and that
// skips expired tokens, so with every token expired it returned "No token found" BEFORE trying the
// HTTP refresh. Only a browser-backed session escaped this, because its visible window kept
// renewing tokens on its own.
//
// Fix: when there is no unexpired token, carry on with the HTTP refresh (previous expiry = epoch)
// instead of giving up. The refresh itself still needs a refresh token in the session; without one
// it fails as before.
//
// Idempotent and fail-loud, like the other patches in this directory.

const fs = require("node:fs");
const path = require("node:path");

const pkgDir = process.argv[2];
if (!pkgDir) {
  throw new Error("Usage: node patch-msteams-refresh-expired.cjs <path to installed msteams-mcp package>");
}

const filePath = path.join(pkgDir, "dist/auth/token-refresh.js");
const source = fs.readFileSync(filePath, "utf8");
const marker = "// patched: no unexpired token -> still try the HTTP refresh";

if (!source.includes(marker)) {
  const original =
    "    const beforeToken = extractSubstrateToken();\n" +
    "    if (!beforeToken) {\n" +
    "        log.warn('token-refresh', 'No Substrate token found in session - cannot refresh, browser login required');\n" +
    "        return err(createError(ErrorCode.AUTH_REQUIRED, 'ACTION REQUIRED: No token found in session. You MUST call teams_login to authenticate.'));\n" +
    "    }\n" +
    "    log.debug('token-refresh', `Current token expires at ${beforeToken.expiry.toISOString()} (${Math.round((beforeToken.expiry.getTime() - Date.now()) / 60000)} mins remaining)`);\n" +
    "    const previousExpiry = beforeToken.expiry;\n";
  if (!source.includes(original)) {
    throw new Error(
      "msteams-mcp: expected refreshTokensViaBrowser() preamble not found in dist/auth/token-refresh.js -- " +
        "upstream reshaped the token refresh; re-verify before patching",
    );
  }
  const replacement =
    "    const beforeToken = extractSubstrateToken();\n" +
    "    " + marker + "\n" +
    "    // (extractSubstrateToken skips expired tokens, so after an idle hour\n" +
    "    // there is none -- which is exactly when a refresh is needed).\n" +
    "    if (!beforeToken) {\n" +
    "        log.info('token-refresh', 'No unexpired Substrate token in session - trying the HTTP refresh');\n" +
    "    }\n" +
    "    const previousExpiry = beforeToken ? beforeToken.expiry : new Date(0);\n";
  fs.writeFileSync(filePath, source.replace(original, replacement), "utf8");
  if (!fs.readFileSync(filePath, "utf8").includes(marker)) {
    throw new Error("msteams-mcp: expired-token refresh patch did not land in dist/auth/token-refresh.js");
  }
}
