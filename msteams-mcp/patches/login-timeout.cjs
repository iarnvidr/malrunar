// Build-time patch for the msteams-mcp package (see ../Dockerfile and ../README.md).
// Fixes an interactive `teams_login --force` that times out (MCP error -32001) before a person has
// even seen the sign-in page.
//
// Cause, in upstream's compiled sources:
//
// 1. dist/tools/auth-tools.js always tries a HEADLESS single-sign-on refresh first, even for
//    forceNew logins. But the forceNew branch, a few lines earlier in the same block, clears the
//    profile's cookies, and headless SSO depends on exactly those cookies. For forceNew the
//    headless attempt is therefore doomed before it starts: it always fails, but only after
//    running to its own timeout.
// 2. dist/cli.js gives the WHOLE teams_login call (the doomed headless attempt AND the human
//    sign-in after it) one shared, hard timeout: `client.callTool(..., { timeout })` with no
//    `maxTotalTimeout` or `resetTimeoutOnProgress`, which the MCP SDK defaults to false. By the
//    time the visible sign-in page appears, a large and unpredictable share of the budget is gone.
//
// Fix (two narrow parts, matching that diagnosis):
//   (a) skip the headless attempt when forceNew is set, so the whole budget goes to the person; the
//       non-forceNew silent recovery path (real long-lived cookies) is untouched;
//   (b) make the CLI's teams_login timeout overridable with MSTEAMS_LOGIN_TIMEOUT_MS and raise its
//       default from 5 to 10 minutes: five minutes is tight for credentials, MFA and a "stay
//       signed in" prompt.
//
// Idempotent, and fail-loud if upstream reshapes either function.

const fs = require("node:fs");
const path = require("node:path");

const pkgDir = process.argv[2];
if (!pkgDir) {
  throw new Error(
    "Usage: node patch-msteams-login-timeout.cjs <path to installed msteams-mcp package>",
  );
}

function patchAuthTools() {
  const filePath = path.join(pkgDir, "dist/tools/auth-tools.js");
  const source = fs.readFileSync(filePath, "utf8");

  const patchedNeedle =
    "if (!input.forceNew) {\n        const headlessManager = await createBrowserContext({ headless: true });";
  if (source.includes(patchedNeedle)) {
    return; // already applied
  }

  const originalNeedle =
    "    // Headless-first strategy:\n" +
    "    // The persistent browser profile retains Microsoft's long-lived session cookies,\n" +
    "    // so headless SSO can succeed even without a session-state file. Always try\n" +
    "    // headless first — even for forceNew. Most recovery scenarios complete silently.\n" +
    "    {\n" +
    "        const headlessManager = await createBrowserContext({ headless: true });";
  if (!source.includes(originalNeedle)) {
    throw new Error(
      "msteams-mcp: expected headless-first teams_login block not found in dist/tools/auth-tools.js -- " +
        "upstream reshaped ensureAuthenticated()/teams_login; re-verify before patching",
    );
  }

  const replacement =
    "    // Headless-first recovery ONLY -- NOT for forceNew (patched). Clearing\n" +
    "    // cookies (below, forceNew branch) and then attempting a headless SSO\n" +
    "    // refresh that depends on those exact cookies is provably futile: it\n" +
    "    // always fails, but not before burning teams_login's one shared timeout\n" +
    "    // budget (see the cli.js patch). Skipped entirely when forceNew is set\n" +
    "    // so a human sign-in gets the full window instead.\n" +
    "    if (!input.forceNew) {\n" +
    "        const headlessManager = await createBrowserContext({ headless: true });";
  fs.writeFileSync(filePath, source.replace(originalNeedle, replacement), "utf8");

  const verify = fs.readFileSync(filePath, "utf8");
  if (!verify.includes(patchedNeedle)) {
    throw new Error(
      "msteams-mcp: forceNew headless-skip patch did not land in dist/tools/auth-tools.js",
    );
  }
}

function patchCliTimeout() {
  const filePath = path.join(pkgDir, "dist/cli.js");
  const source = fs.readFileSync(filePath, "utf8");

  if (source.includes("MSTEAMS_LOGIN_TIMEOUT_MS")) {
    return; // already applied
  }

  const originalNeedle =
    "// Login tool needs a longer timeout for MFA flows\n" +
    "    const timeout = toolName === 'teams_login' ? 5 * 60 * 1000 : undefined; // 5 minutes for login";
  if (!source.includes(originalNeedle)) {
    throw new Error(
      "msteams-mcp: expected teams_login timeout constant not found in dist/cli.js -- " +
        "upstream reshaped callTool()'s timeout handling; re-verify before patching",
    );
  }

  const replacement =
    "// Login tool needs a longer timeout for MFA flows. Overridable via\n" +
    "    // MSTEAMS_LOGIN_TIMEOUT_MS -- this is one hard wall for the whole\n" +
    "    // teams_login call (no maxTotalTimeout/resetTimeoutOnProgress), so it\n" +
    "    // has to cover a full human sign-in including MFA. Default raised from\n" +
    "    // upstream's 5 minutes to 10.\n" +
    "    const timeout = toolName === 'teams_login' ? (Number(process.env.MSTEAMS_LOGIN_TIMEOUT_MS) || 10 * 60 * 1000) : undefined;";
  fs.writeFileSync(filePath, source.replace(originalNeedle, replacement), "utf8");

  const verify = fs.readFileSync(filePath, "utf8");
  if (!verify.includes("MSTEAMS_LOGIN_TIMEOUT_MS")) {
    throw new Error(
      "msteams-mcp: login-timeout override patch did not land in dist/cli.js",
    );
  }
}

patchAuthTools();
patchCliTimeout();
console.log("msteams-mcp: login timeout/headless-race patches applied (or already present).");
