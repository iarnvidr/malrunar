// Graph uses the existing Teams MSAL session. No client registration or sign-in
// is added, and access/rotated refresh tokens stay in this process's memory.
import { jwtExpiryMs } from './auth.mjs';

const GRAPH = 'https://graph.microsoft.com/v1.0';
const SPA_CLIENT = '5e3ce6c0-2b1f-4285-8d4b-75ee78787346';

export class GraphError extends Error {
  constructor(message, status = 502, code = '') {
    super(message); this.status = status; this.code = code;
    this.signedOut = status === 401 || (status === 403 && /invalid.?authentication|interaction.?required|authentication.?required/i.test(code));
  }
}

export function createGraphAuth({ lib, auth, fetchImpl = fetch, now = () => Date.now() }) {
  let access = '', expires = 0, sourceRefresh = '', rotatedRefresh = '', refreshing = null;
  let state = 'starting', detail = '';

  function cacheInfo() {
    const session = lib.sessionStore.readSessionState();
    if (!session) throw new GraphError('Teams sign-in is required for calendar changes', 401);
    const origin = lib.sessionStore.getTeamsOrigin(session);
    const entries = (origin?.localStorage || []).flatMap(item => {
      try { return [JSON.parse(item.value)]; } catch { return []; }
    });
    const refresh = entries.find(entry => entry.credentialType === 'RefreshToken' && entry.secret && entry.clientId);
    const tenant = entries.find(entry => entry.realm)?.realm;
    if (!refresh || !/^[a-zA-Z0-9.-]+$/.test(tenant || '')) throw new GraphError('Teams sign-in is required for calendar changes', 401);
    if (refresh.secret !== sourceRefresh) {
      sourceRefresh = refresh.secret; rotatedRefresh = refresh.secret;
      access = ''; expires = 0;
    }
    const saved = entries.find(entry => entry.credentialType === 'AccessToken' &&
      /graph\.microsoft\.com|00000003-0000-0000-c000-000000000000/.test(entry.target || '') && entry.secret);
    if (!access && saved && jwtExpiryMs(saved.secret) > now() + 60000) {
      access = saved.secret; expires = jwtExpiryMs(access);
    }
    return { refresh, tenant };
  }

  async function renew(force) {
    await auth?.ensure();
    const { refresh, tenant } = cacheInfo();
    if (!force && access && expires > now() + 60000) { state = 'ok'; detail = ''; return access; }
    const response = await fetchImpl(`https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`, {
      method: 'POST', signal: AbortSignal.timeout(15000),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded',
        ...(refresh.clientId === SPA_CLIENT ? { Origin: 'https://teams.microsoft.com' } : {}) },
      body: new URLSearchParams({ grant_type: 'refresh_token', client_id: refresh.clientId,
        refresh_token: rotatedRefresh, scope: 'https://graph.microsoft.com/.default offline_access' }).toString()
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || !body.access_token) {
      // Never echo Azure's response: it can contain session identifiers.
      throw new GraphError(response.status === 400 || response.status === 401 ?
        'Teams sign-in is required for calendar changes' : 'Microsoft token service is unavailable',
      response.status === 400 || response.status === 401 ? 401 : 503, body.error || '');
    }
    access = body.access_token;
    expires = jwtExpiryMs(access) || now() + Number(body.expires_in || 3600) * 1000;
    rotatedRefresh = body.refresh_token || rotatedRefresh;
    state = 'ok'; detail = '';
    return access;
  }

  function token({ force = false } = {}) {
    if (!refreshing) refreshing = renew(force).catch(error => {
      state = error.signedOut ? 'signed-out' : 'unreachable'; detail = error.message;
      throw error;
    }).finally(() => { refreshing = null; });
    return refreshing;
  }

  return { token,
    fail(error) { state = error.signedOut ? 'signed-out' : 'unreachable'; detail = error.message; },
    status: () => ({ state, detail, writable: state === 'ok' && expires > now(), tokenExpiresAt: expires }) };
}

export function createGraphClient({ graphAuth, fetchImpl = fetch, limiter } = {}) {
  async function request(method, path, { body, priority = 2, headers = {} } = {}) {
    if (!Number.isInteger(priority) || priority < 0 || priority > 3) throw new GraphError('Invalid Graph request priority', 400);
    const url = new URL(path.startsWith('https://') ? path : `${GRAPH}${path.startsWith('/') ? path : '/' + path}`);
    if (url.origin !== 'https://graph.microsoft.com' || !url.pathname.startsWith('/v1.0/')) {
      throw new GraphError('Refusing a calendar link outside Microsoft Graph', 400);
    }
    const run = async () => {
      const token = await graphAuth.token();
      const response = await fetchImpl(url, { method, signal: AbortSignal.timeout(30000),
        headers: { Authorization: `Bearer ${token}`, Prefer: 'outlook.timezone="UTC", odata.maxpagesize=250',
          ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        if (response.status === 401) await graphAuth.token({ force: true }).catch(() => {});
        // A write is never retried: the server might already have acted.
        const code = payload.error?.code || '';
        const signedOut = response.status === 401 || (response.status === 403 && /invalid.?authentication|interaction.?required|authentication.?required/i.test(code));
        const error = new GraphError(signedOut ? 'Teams sign-in is required; your changes were not queued' :
          payload.error?.message || `Microsoft Graph returned HTTP ${response.status}`, response.status, payload.error?.code || '');
        if (response.status !== 403 || error.signedOut) graphAuth.fail?.(error);
        throw error;
      }
      if (response.status === 204 || response.status === 202) return null;
      return response.json();
    };
    return limiter ? limiter.run(priority, run) : run();
  }
  return { request, status: graphAuth.status, token: graphAuth.token };
}
