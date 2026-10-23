// Runs msteams-mcp's real refresh code under the guard, with Microsoft mocked.
import fs from 'node:fs';
import { loadLibrary } from '../src/lib.mjs';
import { createAuth } from '../src/auth.mjs';
import { jwt } from './fixture.mjs';

const mode = process.argv[2];
const lib = await loadLibrary();
const fresh = Math.floor(Date.now() / 1000) + 3600;
const calls = [];
globalThis.fetch = async url => {
  calls.push(String(url));
  if (String(url).includes('oauth2/v2.0/token')) {
    return new Response(JSON.stringify({ access_token: jwt({ exp: fresh }), expires_in: 3600, refresh_token: 'rotated-refresh', scope: 'x' }),
      { status: 200, headers: { 'content-type': 'application/json' } });
  }
  return new Response(JSON.stringify({ tokens: { skypeToken: jwt({ exp: fresh, skypeid: 'orgid:11111111-1111-1111-1111-111111111111' }), expiresIn: 86400 } }),
    { status: 200, headers: { 'content-type': 'application/json' } });
};
const auth = createAuth({ lib });

if (mode === 'refresh') {
  const ok = await auth.ensure();
  const rotated = JSON.stringify(lib.sessionStore.readSessionState()).includes('rotated-refresh');
  const target = lib.sessionStore.SESSION_STATE_PATH;
  const refused = [];
  for (const [name, run] of Object.entries({
    append: () => fs.appendFileSync(target, 'x'), rename: () => fs.renameSync(target, target + '.bak'),
    copy: () => fs.copyFileSync(target, target + '.bak'), rm: () => fs.rmSync(target), openw: () => fs.openSync(target, 'w'),
    stream: () => fs.createWriteStream(target), truncate: () => fs.truncateSync(target, 0),
    newfile: () => fs.appendFileSync(target.replace('session-state', 'other'), 'x')
  })) {
    try { run(); refused.push('NOT REFUSED ' + name); } catch (error) { if (error.code !== 'EGUARDED') refused.push(`${name}:${error.code}`); }
  }
  let promised = 'not refused';
  try { await fs.promises.writeFile(target, 'x'); } catch (error) { promised = error.code; }
  console.log(JSON.stringify({ ok, rotated, tokenCalls: calls.length, refused, promised,
    overlay: lib.guard.overlayPaths().map(p => p.split('/').pop()).sort(), remainingMin: Math.round(auth.snapshot().remainingMs / 60000) }));
} else {
  // Newer-file adoption: the MCP rewrites the file between two checks.
  const { spawnSync } = await import('node:child_process');
  const writeAsMcp = seconds => spawnSync(process.execPath, [new URL('./child-mcp-write.mjs', import.meta.url).pathname, String(seconds)], { env: process.env });
  writeAsMcp(7200);
  const first = await auth.ensure();
  const a = Math.round(auth.snapshot().remainingMs / 60000);
  await new Promise(resolve => setTimeout(resolve, 30));
  writeAsMcp(3 * 3600);
  const second = await auth.ensure();
  const b = Math.round(auth.snapshot().remainingMs / 60000);
  console.log(JSON.stringify({ first, a, second, b, tokenCalls: calls.length }));
}
