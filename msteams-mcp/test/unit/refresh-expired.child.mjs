// Child of refresh-expired.test.mjs (run with fetch-mock.mjs preloaded): a session whose access tokens
// have ALL expired, and a valid refresh token, is renewed by the package's own token refresh.
const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG;
const store = await import(`${PKG}/auth/session-store.js`);
const { refreshTokensViaBrowser } = await import(`${PKG}/auth/token-refresh.js`);

const jwt = claims => 'ey' + Buffer.from('{"alg":"none"}').toString('base64url') + '.' + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.sig';
const past = Math.floor(Date.now() / 1000) - 7200;
const CLIENT = '5e3ce6c0-2b1f-4285-8d4b-75ee78787346';
store.writeSessionState({
  cookies: [],
  origins: [{
    origin: 'https://teams.microsoft.com',
    localStorage: [
      { name: 'rt', value: JSON.stringify({ credentialType: 'RefreshToken', secret: 'refresh-0', clientId: CLIENT, homeAccountId: 'h', environment: 'login.microsoftonline.com' }) },
      { name: 'at', value: JSON.stringify({ credentialType: 'AccessToken', realm: '22222222-2222-2222-2222-222222222222', target: 'https://substrate.office.com/SubstrateSearch', secret: jwt({ exp: past }), expiresOn: String(past), clientId: CLIENT, homeAccountId: 'h', environment: 'login.microsoftonline.com' }) },
    ],
  }],
});
const result = await refreshTokensViaBrowser();
console.log(JSON.stringify({ ok: result.ok, method: result.ok ? result.value.method : null, error: result.ok ? null : result.error.message }));
