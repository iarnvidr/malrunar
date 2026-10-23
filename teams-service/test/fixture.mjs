// Throwaway Teams session shaped like the one msteams-mcp saves.
export const jwt = claims => 'ey' + Buffer.from('{"alg":"none"}').toString('base64url') + '.'
  + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.sig';

export const USER_MRI = '8:orgid:11111111-1111-1111-1111-111111111111';

export function session(expSeconds, refresh = 'old-refresh') {
  return {
    cookies: [
      { name: 'skypetoken_asm', value: jwt({ exp: expSeconds, skypeid: 'orgid:11111111-1111-1111-1111-111111111111' }), domain: '.asyncgw.teams.microsoft.com', path: '/', expires: expSeconds },
      { name: 'authtoken', value: 'Bearer%3D' + encodeURIComponent(jwt({ exp: expSeconds })), domain: 'teams.microsoft.com', path: '/', expires: expSeconds }
    ],
    origins: [{
      origin: 'https://teams.cloud.microsoft',
      localStorage: [
        { name: 'rt', value: JSON.stringify({ credentialType: 'RefreshToken', secret: refresh, clientId: '5e3ce6c0-2b1f-4285-8d4b-75ee78787346', homeAccountId: 'h', environment: 'login.microsoftonline.com' }) },
        { name: 'at', value: JSON.stringify({ credentialType: 'AccessToken', realm: 'tenant-1', target: 'https://substrate.office.com/SubstrateSearch', secret: jwt({ exp: expSeconds }), expiresOn: String(expSeconds) }) },
        { name: 'DISCOVER-REGION-GTM', value: JSON.stringify({ item: { chatServiceAfd: 'https://teams.microsoft.com/api/chatsvc/emea', middleTier: 'https://teams.microsoft.com/api/mt/emea' } }) }
      ]
    }]
  };
}

// Writes the session the way the MCP does (library code, no guard).
export async function writeAsMcp(dist, expSeconds, refresh) {
  const store = await import(`${dist}/auth/session-store.js`);
  store.writeSessionState(session(expSeconds, refresh));
}
