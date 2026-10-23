// Build-time patch for the msteams-mcp package (see ../Dockerfile and ../README.md).
// Lets msteams-mcp renew a session that was created by a client other than the Teams web app.
//
// dist/auth/token-refresh-http.js always sends `Origin: https://teams.microsoft.com` when it
// redeems a refresh token. That header is required for the Teams web (single-page-app) client
// and REJECTED for every other client: Entra only allows cross-origin redemption for SPA clients.
// A session whose refresh token belongs to another public client (for example one created by the
// optional device-code module of this component, which signs in as a native client because the web
// client cannot redeem a device code without a client secret, AADSTS7000218) could therefore never
// be renewed. The header is now sent only for the Teams web client id (a public, first-party
// Microsoft application id, not a secret).
//
// Idempotent and fail-loud, like the other patches in this directory.

const fs = require("node:fs");
const path = require("node:path");

const pkgDir = process.argv[2];
if (!pkgDir) {
  throw new Error("Usage: node patch-msteams-refresh-origin.cjs <path to installed msteams-mcp package>");
}

const filePath = path.join(pkgDir, "dist/auth/token-refresh-http.js");
const source = fs.readFileSync(filePath, "utf8");
const patchedNeedle = "...(clientId === TEAMS_WEB_SPA_CLIENT_ID ? { 'Origin': 'https://teams.microsoft.com' } : {}),";

if (!source.includes(patchedNeedle)) {
  const originalNeedle = "                'Origin': 'https://teams.microsoft.com',\n";
  const fnNeedle = "async function refreshAccessToken(tenantId, clientId, refreshToken, scopes) {";
  if (!source.includes(originalNeedle) || !source.includes(fnNeedle)) {
    throw new Error(
      "msteams-mcp: expected refreshAccessToken() Origin header not found in dist/auth/token-refresh-http.js -- " +
        "upstream reshaped the HTTP token refresh; re-verify before patching",
    );
  }
  const patched = source
    .replace(
      fnNeedle,
      "/** Patched: only the Teams web (SPA) client may send a cross-origin Origin. */\n" +
        "const TEAMS_WEB_SPA_CLIENT_ID = '5e3ce6c0-2b1f-4285-8d4b-75ee78787346';\n" +
        fnNeedle,
    )
    .replace(originalNeedle, `                ${patchedNeedle}\n`);
  fs.writeFileSync(filePath, patched, "utf8");
  if (!fs.readFileSync(filePath, "utf8").includes(patchedNeedle)) {
    throw new Error("msteams-mcp: Origin patch did not land in dist/auth/token-refresh-http.js");
  }
}
