// Build-time patch for the msteams-mcp package (see ../Dockerfile and ../README.md).
//
// Upstream hard-codes Chromium (`import { chromium } from 'playwright'`) and picks a Chromium
// *channel* in dist/browser/context.js (getBrowserChannel(): 'msedge' on Windows, 'chrome'
// elsewhere). This patch launches Firefox instead:
//   * the import becomes `import { firefox } from 'playwright'`;
//   * the channel selector becomes getFirefoxExecutablePath(), which returns the value of
//     PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH or undefined (Playwright's own Firefox build);
//   * launchPersistentContext() is called on `firefox` with that executablePath;
//   * the launch error message names Firefox.
//
// Playwright drives Firefox over the "juggler" protocol, which ONLY Playwright's own patched Firefox
// build implements: a stock Mozilla build ignores `-juggler-pipe`, so pointing the executable path
// at one makes the launch hang until Playwright kills it. The component image therefore ships
// Playwright's build and its launcher sets the variable to it.
//
// Mirrors the other patches in this directory: idempotent, and fail-loud if upstream reshapes the
// code this depends on (the build then stops instead of shipping a different behaviour).

const fs = require("node:fs");
const path = require("node:path");

const pkgDir = process.argv[2];
if (!pkgDir) {
  throw new Error(
    "Usage: node patch-msteams-browser-firefox.cjs <path to installed msteams-mcp package>",
  );
}

function patchContextJs() {
  const filePath = path.join(pkgDir, "dist/browser/context.js");
  const source = fs.readFileSync(filePath, "utf8");

  const alreadyPatchedMarker = "import { firefox } from 'playwright';";
  if (source.includes(alreadyPatchedMarker)) {
    return; // already applied
  }

  // Step 1: swap the Playwright browser type import.
  const importNeedle = "import { chromium } from 'playwright';";
  if (!source.includes(importNeedle)) {
    throw new Error(
      "msteams-mcp: expected `import { chromium } from 'playwright'` not found in " +
        "dist/browser/context.js -- upstream reshaped its Playwright import; re-verify before patching",
    );
  }
  let patched = source.replace(importNeedle, "import { firefox } from 'playwright';");

  // Step 2: replace the Chromium-channel selector with a Firefox executable
  // path resolver that defaults to undefined, i.e. Playwright's own bundled
  // build -- the only one that implements juggler. The Dockerfile installs
  // that build explicitly. PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH remains as an
  // operator override, but pointing it at a stock Mozilla build will hang.
  const channelFnNeedle =
    "/**\n" +
    " * Determines the browser channel to use based on the platform.\n" +
    " * - Windows: Use Microsoft Edge (always installed on Windows 10+)\n" +
    " * - macOS/Linux: Use Chrome\n" +
    " *\n" +
    " * @returns The browser channel name for Playwright\n" +
    " */\n" +
    "function getBrowserChannel() {\n" +
    "    return process.platform === 'win32' ? 'msedge' : 'chrome';\n" +
    "}";
  if (!patched.includes(channelFnNeedle)) {
    throw new Error(
      "msteams-mcp: expected getBrowserChannel() not found in dist/browser/context.js -- " +
        "upstream reshaped browser-channel selection; re-verify before patching",
    );
  }
  const channelFnReplacement =
    "/**\n" +
    " * Resolves the Firefox binary to launch. Defaults to Playwright's OWN\n" +
    " * bundled build, because Playwright drives Firefox over the juggler\n" +
    " * protocol that only that build implements (patched -- see\n" +
    " * patch-msteams-browser-firefox.cjs).\n" +
    " *\n" +
    " * @returns A Firefox executable path, or undefined to use Playwright's own.\n" +
    " */\n" +
    "function getFirefoxExecutablePath() {\n" +
    "    return process.env.PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH || undefined;\n" +
    "}";
  patched = patched.replace(channelFnNeedle, channelFnReplacement);

  const channelVarNeedle = "    const channel = getBrowserChannel();";
  if (!patched.includes(channelVarNeedle)) {
    throw new Error(
      "msteams-mcp: expected `const channel = getBrowserChannel();` not found in " +
        "dist/browser/context.js -- re-verify before patching",
    );
  }
  patched = patched.replace(
    channelVarNeedle,
    "    const executablePath = getFirefoxExecutablePath();",
  );

  // Step 3: launch Firefox instead of Chromium, with executablePath instead
  // of channel -- Playwright's Firefox has no channel concept the way
  // Chromium's chrome/msedge/chrome-beta channels do.
  const launchNeedle =
    "        const context = await chromium.launchPersistentContext(BROWSER_PROFILE_DIR, {\n" +
    "            headless: opts.headless,\n" +
    "            channel,\n" +
    "            viewport: opts.viewport,\n" +
    "            acceptDownloads: false,\n" +
    "        });";
  if (!patched.includes(launchNeedle)) {
    throw new Error(
      "msteams-mcp: expected chromium.launchPersistentContext(...) call not found in " +
        "dist/browser/context.js -- re-verify before patching",
    );
  }
  patched = patched.replace(
    launchNeedle,
    "        const context = await firefox.launchPersistentContext(BROWSER_PROFILE_DIR, {\n" +
      "            headless: opts.headless,\n" +
      "            ...(executablePath ? { executablePath } : {}),\n" +
      "            viewport: opts.viewport,\n" +
      "            acceptDownloads: false,\n" +
      "        });",
  );

  // Step 4: fix the error message upstream builds from `channel` when launch
  // fails outright (not the SingletonLock recovery path, which is a separate gap).
  const errorMsgNeedle =
    "        const browserName = channel === 'msedge' ? 'Microsoft Edge' : 'Google Chrome';\n" +
    "        const installHint = channel === 'msedge'\n" +
    "            ? 'Edge should be pre-installed on Windows. Try updating Windows or reinstalling Edge.'\n" +
    "            : 'Install Chrome from https://www.google.com/chrome/ or run: npx playwright install chromium';";
  if (!patched.includes(errorMsgNeedle)) {
    throw new Error(
      "msteams-mcp: expected channel-based browserName/installHint not found in " +
        "dist/browser/context.js -- re-verify before patching",
    );
  }
  patched = patched.replace(
    errorMsgNeedle,
    "        const browserName = 'Mozilla Firefox';\n" +
      "        const installHint = 'Install the Playwright Firefox build (npx playwright install firefox) or set " +
      "PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH to it; a stock Mozilla Firefox cannot be driven.';",
  );

  fs.writeFileSync(filePath, patched, "utf8");

  const verify = fs.readFileSync(filePath, "utf8");
  if (
    !verify.includes(alreadyPatchedMarker) ||
    !verify.includes("firefox.launchPersistentContext(BROWSER_PROFILE_DIR") ||
    !verify.includes("getFirefoxExecutablePath()")
  ) {
    throw new Error(
      "msteams-mcp: Firefox browser patch did not fully land in dist/browser/context.js",
    );
  }
}

patchContextJs();
console.log("msteams-mcp: Firefox browser patch applied (or already present).");
