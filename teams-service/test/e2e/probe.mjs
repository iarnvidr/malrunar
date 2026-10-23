// A small HTTP probe for the e2e scripts (the test images have no curl).
//   probe.mjs <data dir> <expectation...>
// Waits for the Teams service and PocketBase, then checks the API the way a client would.
import fs from 'node:fs';
import path from 'node:path';

const dataDir = process.argv[2];
const service = `http://127.0.0.1:${process.env.MALRUNAR_TEAMS_SERVICE_PORT || 18390}`;
const pb = `http://127.0.0.1:${process.env.MALRUNAR_TEAMS_SERVICE_PB_PORT || 18391}`;
const fail = text => { console.error('PROBE FAILED: ' + text); process.exit(1); };
const wait = async (url, what) => {
  for (let attempt = 0; attempt < 120; attempt++) {
    try { if ((await fetch(url)).ok) return; } catch { /* not up yet */ }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  fail(`${what} did not come up at ${url}`);
};
await wait(`${pb}/api/health`, 'PocketBase');
await wait(`${service}/health`, 'the Teams service');

// 1. the API wants the bearer token, and the token file the service created works
const tokenFile = path.join(dataDir, 'service-token');
if (!fs.existsSync(tokenFile)) fail(`no token file ${tokenFile}`);
if ((fs.statSync(tokenFile).mode & 0o077) !== 0) fail('the token file is readable by others');
const token = fs.readFileSync(tokenFile, 'utf8').trim();
if (token.length < 32) fail('the generated token is shorter than 32 characters');
if ((await fetch(`${service}/chats`)).status !== 401) fail('/chats without a token is not 401');
if ((await fetch(`${service}/chats`, { headers: { Authorization: 'Bearer wrong' } })).status !== 401) fail('/chats with a wrong token is not 401');
const chats = await fetch(`${service}/chats`, { headers: { Authorization: `Bearer ${token}` } });
if (chats.status !== 200) fail(`/chats with the token answered ${chats.status}`);
const body = await chats.json();
if (!Array.isArray(body.chats)) fail('/chats did not return a chat list');
const status = await (await fetch(`${service}/status`, { headers: { Authorization: `Bearer ${token}` } })).json();
console.log('status:', JSON.stringify({ auth: status.auth?.state, haveTokens: status.auth?.haveTokens, counts: status.counts }));
if (status.auth?.haveTokens !== false) fail('with no session the service must report that it has no tokens');

// 2. PocketBase has the schema of the migrations compiled into the binary, and rejects anonymous reads
const credentials = JSON.parse(fs.readFileSync(path.join(dataDir, 'pb-credentials.json'), 'utf8'));
if (fs.statSync(path.join(dataDir, 'pb-credentials.json')).mode & 0o077) fail('pb-credentials.json is readable by others');
const login = await fetch(`${pb}/api/collections/_superusers/auth-with-password`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ identity: credentials.identity, password: credentials.password }) });
if (!login.ok) fail(`superuser login failed (${login.status})`);
const { token: pbToken } = await login.json();
const collections = await (await fetch(`${pb}/api/collections?perPage=200`, { headers: { Authorization: pbToken } })).json();
const names = new Set(collections.items.filter(c => !c.system).map(c => c.name));
for (const name of ['chats', 'messages', 'people', 'media', 'state', 'calendar_events', 'push_subscriptions', 'push_state']) {
  if (!names.has(name)) fail(`collection ${name} is missing: ${[...names].join(', ')}`);
}
const calendar = collections.items.find(c => c.name === 'calendar_events');
if (!calendar.fields.some(f => f.name === 'calendar_id')) fail('the subscribed-calendars migration did not run');
const anonymous = await fetch(`${pb}/api/collections/messages/records`);
if (anonymous.ok) fail('an anonymous client can read the messages collection');
console.log('collections:', [...names].sort().join(', '));
console.log('probe ok');
