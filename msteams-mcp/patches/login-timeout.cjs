// Keep the login timeout configurable and scope forced reset to this package's authentication data.
const fs = require("node:fs");
const path = require("node:path");
const dist = path.join(process.argv[2], "dist");

function replaceOnce(name, from, to) {
  const file = path.join(dist, name);
  const source = fs.readFileSync(file, "utf8");
  if (source.includes(to)) return;
  if (source.split(from).length !== 2) throw new Error("msteams-mcp login integration changed: " + name);
  fs.writeFileSync(file, source.replace(from, to));
}

replaceOnce("cli.js", "const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;",
  "const LOGIN_TIMEOUT_MS = Number(process.env.MSTEAMS_LOGIN_TIMEOUT_MS) || 10 * 60 * 1000;");
replaceOnce("auth/session-store.js", "fs.rmSync(CONFIG_DIR, { recursive: true, force: true });",
  "for (const file of [SESSION_STATE_PATH, TOKEN_CACHE_PATH, path.join(CONFIG_DIR, 'browser-profile')]) fs.rmSync(file, { recursive: true, force: true });");
