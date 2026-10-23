// Token handling for the Teams service.
//
// The MCP's session-state.json is the source of truth and the MCP is its only
// writer. This service reads it (through msteams-mcp's own extractors), lets
// msteams-mcp's own refresh logic mint new tokens, and keeps the result in the
// in-memory overlay of guard.mjs, so rotated tokens never reach the file. When
// the chain fails, or when the MCP has written a newer file, the overlay is
// dropped and the file is read again. refreshTokensViaBrowser (which starts a
// browser against the shared profile) is never called.

export const REFRESH_MARGIN_MS = 10 * 60 * 1000;
const FAILED_RETRY_MS = 60 * 1000;

export function jwtExpiryMs(token) {
  try {
    const payload = JSON.parse(Buffer.from(String(token).split('.')[1], 'base64url').toString('utf8'));
    return typeof payload.exp === 'number' ? payload.exp * 1000 : 0;
  } catch { return 0; }
}

export function createAuth({ lib, now = () => Date.now(), log = () => {} }) {
  const sessionFile = lib.sessionStore.SESSION_STATE_PATH;
  let seenMtime = null;
  let inflight = null;
  let lastAttempt = 0;
  const status = { state: 'starting', since: now(), detail: '', refreshedAt: 0, mode: 'file' };

  const clearCaches = () => { lib.tokenExtractor.clearTokenCache(); lib.guards.clearRegionCache(); };

  function snapshot() {
    let auth = null;
    try { auth = lib.tokenExtractor.extractMessageAuth(); } catch { auth = null; }
    if (!auth) return { ok: false, remainingMs: 0 };
    const remaining = Math.min(jwtExpiryMs(auth.skypeToken) - now(), jwtExpiryMs(auth.authToken) - now());
    return { ok: true, remainingMs: remaining, userMri: auth.userMri, skypeToken: auth.skypeToken };
  }

  function adoptNewerFile() {
    const mtime = lib.guard.diskMtimeMs(sessionFile);
    if (seenMtime !== null && mtime !== seenMtime) {
      // The MCP wrote the file: its tokens are at least as new as ours.
      lib.guard.dropOverlay();
      clearCaches();
      log('info', 'The MCP wrote a newer session file; using it instead of the in-memory tokens');
    }
    seenMtime = mtime;
  }

  function setState(state, detail = '') {
    if (status.state !== state) status.since = now();
    status.state = state;
    status.detail = detail;
  }

  function failureKind(error) {
    const code = String(error?.code || '');
    return code === 'AUTH_EXPIRED' || code === 'AUTH_REQUIRED' ? 'signed-out' : 'unreachable';
  }

  async function refreshOnce() {
    const result = await lib.tokenRefresh.refreshTokensViaHttp();
    clearCaches();
    return result;
  }

  async function renew() {
    lastAttempt = now();
    let result = await refreshOnce();
    if (result.ok) {
      status.refreshedAt = now();
      status.mode = 'memory';
      setState('ok');
      return true;
    }
    // Our own chain failed: read the MCP's file again, which may hold tokens the
    // MCP has refreshed since.
    lib.guard.dropOverlay();
    clearCaches();
    status.mode = 'file';
    if (snapshot().remainingMs > 2 * 60 * 1000) { setState('ok'); return true; }
    result = await refreshOnce();
    if (result.ok) {
      status.refreshedAt = now();
      status.mode = 'memory';
      setState('ok');
      return true;
    }
    const kind = failureKind(result.error);
    setState(kind, String(result.error?.message || 'Token refresh failed').slice(0, 300));
    log('warn', `Token refresh failed (${kind}): ${status.detail}`);
    return false;
  }

  async function ensure({ force = false } = {}) {
    if (inflight) return inflight;
    adoptNewerFile();
    const current = snapshot();
    const usable = current.ok && current.remainingMs > REFRESH_MARGIN_MS;
    if (usable && !force) { if (status.state !== 'ok') setState('ok'); return true; }
    if (!force && status.state !== 'ok' && now() - lastAttempt < FAILED_RETRY_MS) return current.ok && current.remainingMs > 0;
    inflight = renew().finally(() => { inflight = null; });
    return inflight;
  }

  return {
    ensure,
    // A request was refused with 401/403 although the tokens looked fresh.
    refused: () => ensure({ force: true }),
    snapshot,
    status: () => {
      const current = snapshot();
      return { ...status, tokenExpiresAt: current.ok ? Math.round((now() + current.remainingMs)) : 0, haveTokens: current.ok };
    },
    userMri: () => snapshot().userMri || null
  };
}
