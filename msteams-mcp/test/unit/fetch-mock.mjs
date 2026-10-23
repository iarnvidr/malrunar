// Preloaded (node --import) into device-login.mjs: replaces fetch with a scripted Microsoft. Every
// request is appended to $MOCK_LOG as JSON (one per line) so the test can assert on it. Nothing here
// resembles a real account: the identifiers are made up.
import fs from 'node:fs';

const log = process.env.MOCK_LOG;
const jwt = claims => 'ey' + Buffer.from('{"alg":"none"}').toString('base64url') + '.' + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.sig';
const exp = Math.floor(Date.now() / 1000) + 3600;
let polls = 0;

globalThis.fetch = async (url, init = {}) => {
  const target = String(url);
  const body = typeof init.body === 'string' ? init.body : init.body ? String(init.body) : '';
  const params = Object.fromEntries(new URLSearchParams(body));
  fs.appendFileSync(log, JSON.stringify({ url: target, method: init.method, headers: init.headers || {}, params }) + '\n');
  const reply = (status, value) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
  if (target.endsWith('/oauth2/v2.0/devicecode')) {
    return reply(200, { user_code: 'ABCD1234', device_code: 'device-code-1', verification_uri: 'https://example.invalid/device', interval: 0, expires_in: 60 });
  }
  if (target.includes('/oauth2/v2.0/token')) {
    if (params.grant_type === 'urn:ietf:params:oauth:grant-type:device_code') {
      if (++polls < 2) return reply(400, { error: 'authorization_pending' });
      return reply(200, {
        access_token: jwt({ tid: '22222222-2222-2222-2222-222222222222', oid: '33333333-3333-3333-3333-333333333333', exp }),
        refresh_token: 'refresh-1', expires_in: 3600, scope: 'https://api.spaces.skype.com/user_impersonation',
      });
    }
    // a refresh_token grant, once per resource scope
    const scope = params.scope.includes('substrate.office.com') ? 'https://substrate.office.com/SubstrateSearch-Internal.ReadWrite' : params.scope;
    return reply(200, { access_token: jwt({ exp, scp: scope }), refresh_token: 'refresh-2', expires_in: 3600, scope });
  }
  if (target.includes('/api/authsvc/v1.0/authz')) {
    return reply(200, {
      tokens: { skypeToken: jwt({ exp, skypeid: 'orgid:33333333-3333-3333-3333-333333333333' }), expiresIn: 86400 },
      region: 'emea', partition: 'emea01',
      regionGtms: { chatServiceAfd: 'https://example.invalid/api/chatsvc/emea', middleTier: 'https://example.invalid/api/mt/emea' },
    });
  }
  return reply(404, { error: 'unexpected request to ' + target });
};
