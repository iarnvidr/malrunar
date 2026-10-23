// Loads msteams-mcp's own API modules (dist/*.js of the package in the msteams-mcp
// component) after the session guard is in place.
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';
import { installSessionGuard } from './guard.mjs';

// msteams-mcp derives its session-file key with scryptSync on EVERY read of the
// session (about 0.1-0.3 s of CPU each, several times per Teams request). The
// derivation is a pure function of its arguments, so remember the results.
let memoised = false;
export function memoiseKeyDerivation() {
  if (memoised) return;
  memoised = true;
  const original = crypto.scryptSync;
  const results = new Map();
  crypto.scryptSync = function scryptSync(password, salt, keylen, options) {
    const key = JSON.stringify([String(password), String(salt), keylen, options || null]);
    if (!results.has(key)) results.set(key, original.call(crypto, password, salt, keylen, options));
    return Buffer.from(results.get(key));
  };
  syncBuiltinESMExports();
}

export const DEFAULT_DIST = '/opt/malrunar/msteams-mcp/app/dist';

export async function loadLibrary({ dist = process.env.MALRUNAR_TEAMS_SERVICE_MCP_DIST || DEFAULT_DIST } = {}) {
  // The library derives its state directory from the home directory at import
  // time; guard exactly that directory before anything can write to it.
  const stateDir = process.env.MALRUNAR_TEAMS_SERVICE_MCP_STATE_DIR || path.join(os.homedir(), '.teams-mcp-server');
  const guard = installSessionGuard([stateDir]);
  memoiseKeyDerivation();
  const base = pathToFileURL(path.join(dist, path.sep)).href;
  const load = relative => import(base + relative);
  const [sessionStore, tokenExtractor, tokenRefresh, chatsvc, apiConfig, http, guards, profile] = await Promise.all([
    load('auth/session-store.js'), load('auth/token-extractor.js'), load('auth/token-refresh-http.js'),
    load('api/chatsvc-api.js'), load('utils/api-config.js'), load('utils/http.js'), load('utils/auth-guards.js'),
    load('api/profile-api.js')
  ]);
  const inside = guard.roots.some(root => sessionStore.CONFIG_DIR === root || sessionStore.CONFIG_DIR.startsWith(root + path.sep));
  if (!inside) throw new Error(`msteams-mcp keeps its session in ${sessionStore.CONFIG_DIR}, which the guard does not cover`);
  return { guard, sessionStore, tokenExtractor, tokenRefresh, chatsvc, apiConfig, http, guards, profile, dist };
}
