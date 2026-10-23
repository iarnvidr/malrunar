// Entry point: the Teams service, a local PocketBase copy of Teams chats and calendar behind a small HTTP API.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { loadLibrary } from './lib.mjs';
import { createAuth } from './auth.mjs';
import { PocketBase } from './pb.mjs';
import { PbStore } from './store.mjs';
import { createLimiter, createTeams } from './teams.mjs';
import { createMediaCache } from './media.mjs';
import { createSync } from './sync.mjs';
import { createServer } from './server.mjs';
import { createGraphAuth, createGraphClient } from './calendar-auth.mjs';
import { createCalendar } from './calendar.mjs';
import { createFileClient } from './files.mjs';
import { mediaKey } from './messages.mjs';
import { ConfigError, IdentityConfig } from './forward-auth.mjs';

// Settings: MALRUNAR_TEAMS_SERVICE_* variables, documented in ../README.md.
const env = name => process.env[`MALRUNAR_TEAMS_SERVICE_${name}`];
const dataDir = env('DATA_DIR') || path.join(os.homedir(), '.local', 'share', 'malrunar-teams-service');
const port = Number(env('PORT') || 18390);
const bind = env('BIND') || '127.0.0.1';
const pbUrl = env('PB_URL') || `http://127.0.0.1:${Number(env('PB_PORT') || 18391)}`;
const levels = { error: 0, warn: 1, info: 2, debug: 3 };
const threshold = levels[env('LOG_LEVEL') || 'info'] ?? 2;
const log = (level, message) => { if ((levels[level] ?? 2) <= threshold) console.log(JSON.stringify({ t: new Date().toISOString(), level, message })); };

// The bearer token of the HTTP API: MALRUNAR_TEAMS_SERVICE_TOKEN, else the file named by
// MALRUNAR_TEAMS_SERVICE_TOKEN_FILE, else <data dir>/service-token, created with a random token (mode 0600)
// when it does not exist. The variable is removed so nothing started from this process inherits it.
function resolveToken() {
  const fromEnv = env('TOKEN');
  delete process.env.MALRUNAR_TEAMS_SERVICE_TOKEN;
  if (fromEnv) return fromEnv;
  const file = env('TOKEN_FILE') || path.join(dataDir, 'service-token');
  if (!fs.existsSync(file) && !env('TOKEN_FILE')) {
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    fs.writeFileSync(file, crypto.randomBytes(32).toString('hex') + '\n', { mode: 0o600, flag: 'wx' });
  }
  try { return fs.readFileSync(file, 'utf8').trim(); } catch (error) { throw new Error(`MALRUNAR_TEAMS_SERVICE_TOKEN_FILE ${file}: ${error.code || error.message}`); }
}
const token = resolveToken();
if (token.length < 32) { console.error('The Teams service token (MALRUNAR_TEAMS_SERVICE_TOKEN, or the file it is read from) is missing or shorter than 32 characters'); process.exit(78); }

// Identity from an authenticating reverse proxy (MALRUNAR_TEAMS_SERVICE_USER_HEADER, GROUPS_HEADER, NAME_HEADER,
// EMAIL_HEADER, TRUSTED_PROXIES, PROXY_SECRET_HEADER, PROXY_SECRET_FILE). Additional to the bearer token.
let identity;
try { identity = IdentityConfig.fromEnv(process.env, 'MALRUNAR_TEAMS_SERVICE_'); }
catch (error) { if (!(error instanceof ConfigError)) throw error; console.error(error.message); process.exit(78); }
const identityWarning = identity.startupWarning();

const credentials = JSON.parse(fs.readFileSync(path.join(dataDir, 'pb-credentials.json'), 'utf8'));
const pb = new PocketBase({ url: pbUrl, identity: credentials.identity, password: credentials.password });

async function waitForPocketBase() {
  for (let attempt = 0; attempt < 120; attempt++) {
    try { const response = await fetch(`${pbUrl}/api/health`); if (response.ok) { await pb.authenticate(); return; } } catch { /* not up yet */ }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('PocketBase did not become ready');
}
await waitForPocketBase();
// PocketBase writes a consistent zip of its data every night (kept: the last two) under its data
// directory, so a file-level backup of that directory picks up the zips rather than a live database.
// MALRUNAR_TEAMS_SERVICE_BACKUP_CRON overrides the schedule; `off` leaves PocketBase's setting alone.
const backupCron = env('BACKUP_CRON') || '20 3 * * *';
if (backupCron !== 'off') {
  try { await pb.request('PATCH', '/api/settings', { body: { backups: { cron: backupCron, cronMaxKeep: 2 } } }); } catch (error) { log('warn', `Could not schedule PocketBase backups: ${error.message}`); }
}

const store = new PbStore(pb);
await store.init();
const lib = await loadLibrary();
const auth = createAuth({ lib, log });
const limiter = createLimiter({ gapMs: Number(env('REQUEST_GAP_MS') || 250) });
const teams = createTeams({ lib, auth, limiter, log });
const graphAuth = createGraphAuth({ lib, auth });
const graph = createGraphClient({ graphAuth, limiter });
const files = createFileClient({ token: graphAuth.token });
const media = createMediaCache({ dir: path.join(dataDir, 'media'), store, skypeToken: teams.skypeToken, fileDownload: files.download, log });
const sync = createSync({ store, teams, media, auth, log });
const calendar = createCalendar({ pb, graph, log });
await calendar.init();
const myName = () => { try { return lib.tokenExtractor.getUserDisplayName() || ''; } catch { return ''; } };

async function registerArchivedFiles() {
  if (store.getState('file_archive_registered')) return;
  for (let page = 1; ; page++) {
    const rows = await pb.list('messages', { filter: "deleted = false && files != '[]'", fields: 'chat_id,msg_id,files', perPage: 200, page });
    for (const row of rows) await media.register(row.chat_id, row.msg_id, (row.files || []).filter(file => file.url).map(file => ({ url: file.url, key: mediaKey(file.url) })));
    if (rows.length < 200) break;
  }
  await store.setState('file_archive_registered', true);
}
if (identityWarning) log('warn', identityWarning);
const server = createServer({ token, store, sync, teams, auth, media, files, calendar, myName, identity, log });
server.listen(port, bind, () => {
  log('info', `Teams service listening on ${bind}:${port}`);
  auth.ensure().then(() => sync.start()).catch(() => sync.start());
  calendar.start();
  registerArchivedFiles().catch(() => log('warn', 'Archived file registration will retry on the next service start'));
});

for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => { sync.stop(); calendar.stop(); server.close(); setTimeout(() => process.exit(0), 500).unref(); });
process.on('unhandledRejection', error => log('error', `Unhandled rejection: ${String(error?.stack || error).slice(0, 500)}`));
