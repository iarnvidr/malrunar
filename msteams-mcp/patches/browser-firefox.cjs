// Use upstream's Firefox support while retaining Malrunar's existing browser profile.
const fs = require("node:fs");
const path = require("node:path");
const file = path.join(process.argv[2], "dist/browser/context.js");
let source = fs.readFileSync(file, "utf8");
if (source.includes("PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH")) process.exit(0);

function replaceOnce(from, to) {
  if (source.split(from).length !== 2) throw new Error("msteams-mcp Firefox integration changed: " + from);
  source = source.replace(from, to);
}

replaceOnce("const FIREFOX_PROFILE_DIR = path.join(CONFIG_DIR, 'browser-profile-firefox');",
  "const FIREFOX_PROFILE_DIR = path.join(CONFIG_DIR, 'browser-profile');");
replaceOnce("firefox.launchPersistentContext(FIREFOX_PROFILE_DIR, options)",
  "firefox.launchPersistentContext(FIREFOX_PROFILE_DIR, { ...options, executablePath: process.env.PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH || undefined })");
fs.writeFileSync(file, source);
