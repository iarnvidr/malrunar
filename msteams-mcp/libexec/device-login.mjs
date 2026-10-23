// OPTIONAL and OFF BY DEFAULT: device-code sign-in for msteams-mcp (no browser needed).
//
// It prints a code to approve at the address Microsoft names (from any phone), polls Entra, seeds
// msteams-mcp's own encrypted session store with the refresh token and lets the package's own HTTP
// refresh path mint the Substrate, Skype and chat tokens.
//
// WHY IT IS OFF. To redeem a device code without a client secret, this signs in as the Microsoft Teams
// DESKTOP application: Microsoft's first-party public client, whose application id is public but which
// is not registered to you. Using it from a script is outside what Microsoft documents, may breach the
// terms that apply to you, and may be blocked or flagged by your tenant (Conditional Access, sign-in
// logs, device-code-flow blocking). It runs only when MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1, set by you
// after reading the README section "Device-code sign-in". The patch that lets the package renew such a
// session (patches/refresh-origin.cjs) is generic and always applied; this module is the only code that
// signs in this way.
//
//   device-login.mjs [--region-only]
//     --region-only  refresh the region configuration of the existing session; no sign-in
// Settings: MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN (must be 1), MALRUNAR_MSTEAMS_MCP_TENANT (default
// "organizations"), MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID (default: the Teams desktop client),
// MALRUNAR_MSTEAMS_MCP_PKG (the package's dist directory, set by the launcher).
if (process.env.MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN !== '1') {
  console.error('device-code sign-in is off: set MALRUNAR_MSTEAMS_MCP_DEVICE_LOGIN=1 after reading the README section "Device-code sign-in"');
  process.exit(78);
}

const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG || '/opt/malrunar/msteams-mcp/app/dist';
const { writeSessionState, readSessionState, getTeamsOrigin } = await import(`${PKG}/auth/session-store.js`);
const { refreshTokensViaHttp } = await import(`${PKG}/auth/token-refresh-http.js`);

const TENANT = process.env.MALRUNAR_MSTEAMS_MCP_TENANT || 'organizations';
// The Teams desktop application (public native client). The web client cannot redeem a device code
// without a client secret (AADSTS7000218), and its browser-bound refresh tokens are capped at 24 hours.
// Refreshing this client's tokens needs patches/refresh-origin.cjs.
const CLIENT = process.env.MALRUNAR_MSTEAMS_MCP_DEVICE_CLIENT_ID || '1fec8e78-bce4-4aaf-ab1b-5451cc387264';
const SCOPE = 'https://api.spaces.skype.com/.default offline_access openid profile';
const AAD = 'https://login.microsoftonline.com';

async function post(url, params) {
  const response = await fetch(url, { method: 'POST', body: new URLSearchParams(params) });
  return response.json();
}
const claimsOf = (jwt) => JSON.parse(Buffer.from(jwt.split('.')[1], 'base64url').toString());

// The region and partition service URLs (chat, middle tier, calendar, ...) that msteams-mcp reads from
// the web app's DISCOVER-REGION-GTM localStorage item, which a device-code session never gets. Teams'
// authsvc returns the same map as regionGtms when it exchanges the Skype Spaces token, so write it in
// that item's shape ({ item: {...} }).
async function ensureRegion() {
  const state = readSessionState();
  const storage = getTeamsOrigin(state)?.localStorage;
  if (!storage) throw new Error('no Teams session to add the region to');
  const spaces = storage.map((item) => { try { return JSON.parse(item.value); } catch { return null; } })
    .find((entry) => entry?.credentialType === 'AccessToken' && entry.target?.includes('api.spaces.skype.com'));
  if (!spaces) throw new Error('no Skype Spaces token in the session');
  const response = await fetch('https://teams.microsoft.com/api/authsvc/v1.0/authz', {
    method: 'POST', headers: { Authorization: `Bearer ${spaces.secret}`, 'Content-Type': 'application/json' }, body: '{}',
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.regionGtms?.chatServiceAfd) throw new Error(`authsvc gave no region config (HTTP ${response.status})`);
  const name = 'DISCOVER-REGION-GTM';
  const value = JSON.stringify({ item: body.regionGtms });
  const at = storage.findIndex((item) => item.name.includes(name));
  if (at >= 0) storage[at].value = value; else storage.push({ name, value });
  writeSessionState(state);
  return `${body.region}/${body.partition}`;
}

if (process.argv.includes('--region-only')) {
  console.log(`REGION ${await ensureRegion()}`);
  console.log('DONE');
  process.exit(0);
}

const deviceCode = await post(`${AAD}/${TENANT}/oauth2/v2.0/devicecode`, { client_id: CLIENT, scope: SCOPE });
if (!deviceCode.user_code) { console.log('DEVICECODE_ERROR', deviceCode.error_description || deviceCode.error); process.exit(1); }
console.log(`USERCODE: ${deviceCode.user_code}`);
console.log(`Open ${deviceCode.verification_uri} and enter ${deviceCode.user_code}`);

let token = null;
let interval = deviceCode.interval * 1000;
const deadline = Date.now() + deviceCode.expires_in * 1000;
while (Date.now() < deadline) {
  await new Promise((resolve) => setTimeout(resolve, interval));
  const result = await post(`${AAD}/${TENANT}/oauth2/v2.0/token`, {
    grant_type: 'urn:ietf:params:oauth:grant-type:device_code', client_id: CLIENT, device_code: deviceCode.device_code,
  });
  if (result.access_token) { token = result; break; }
  if (result.error === 'authorization_pending') continue;
  if (result.error === 'slow_down') { interval += 5000; continue; }
  console.log('TOKEN_ERROR', result.error, (result.error_description || '').slice(0, 200));
  process.exit(1);
}
if (!token) { console.log('TIMEOUT'); process.exit(1); }
if (!token.refresh_token) { console.log('TOKEN_ERROR no refresh_token'); process.exit(1); }

const claims = claimsOf(token.access_token);
const tid = claims.tid;
const oid = claims.oid;
const homeAccountId = `${oid}.${tid}`;
const environment = 'login.windows.net';
const now = Math.floor(Date.now() / 1000);
const refreshToken = { credentialType: 'RefreshToken', homeAccountId, environment, clientId: CLIENT, secret: token.refresh_token, lastUpdatedAt: String(Date.now()) };
const accessToken = {
  credentialType: 'AccessToken', homeAccountId, environment, clientId: CLIENT, realm: tid,
  target: token.scope, tokenType: 'Bearer', secret: token.access_token,
  expiresOn: String(now + token.expires_in), extendedExpiresOn: String(now + (token.ext_expires_in || token.expires_in)), cachedAt: String(now),
};
const state = {
  cookies: [],
  origins: [{
    origin: 'https://teams.microsoft.com',
    localStorage: [
      { name: `${homeAccountId}-${environment}-refreshtoken-${CLIENT}----`, value: JSON.stringify(refreshToken) },
      { name: `${homeAccountId}-${environment}-accesstoken-${CLIENT}-${tid}-${token.scope.toLowerCase()}`, value: JSON.stringify(accessToken) },
    ],
  }],
};
// Keep the previous session's cookies so a visible browser that mirrors them is unaffected, but
// replace the Teams origin's MSAL cache with the fresh one.
const previous = readSessionState();
if (previous?.cookies) state.cookies = previous.cookies;
writeSessionState(state);
console.log('SESSION_SEEDED');

const refreshed = await refreshTokensViaHttp();
if (!refreshed.ok) { console.log('REFRESH_ERROR', refreshed.error?.message); process.exit(1); }
console.log('REFRESH_OK', JSON.stringify(refreshed.value).slice(0, 300));
try {
  console.log(`REGION ${await ensureRegion()}`);
} catch (error) {
  console.log('REGION_ERROR', error.message);
  process.exit(1);
}
console.log('DONE');
