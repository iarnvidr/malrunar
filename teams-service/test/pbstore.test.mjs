// The PocketBase-backed store against a real PocketBase built from ./pocketbase.
// Set MALRUNAR_TEAMS_SERVICE_TEST_PB_BIN to the built binary; the tests are skipped without it.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { PocketBase } from '../src/pb.mjs';
import { PbStore } from '../src/store.mjs';
import { createSync } from '../src/sync.mjs';
import { createServer } from '../src/server.mjs';
import { createCalendar } from '../src/calendar.mjs';
import { GraphError } from '../src/calendar-auth.mjs';
import { FakeWorld, ONE_TO_ONE, GROUP, raw, fakeAuth, fakeMedia } from '../dev/fakes.mjs';

const BIN = process.env.MALRUNAR_TEAMS_SERVICE_TEST_PB_BIN;
const T0 = Date.parse('2026-10-13T08:00:00Z');
const at = minutes => T0 + minutes * 60000;

async function startPocketBase() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'malrunar-teams-pb-'));
  const port = 20000 + Math.floor(Math.random() * 20000);
  const created = spawnSync(BIN, ['superuser', 'upsert', 'svc@local.invalid', 'a-long-test-password', `--dir=${dir}`], { encoding: 'utf8' });
  assert.equal(created.status, 0, created.stderr);
  const child = spawn(BIN, ['serve', `--http=127.0.0.1:${port}`, `--dir=${dir}`], { stdio: 'ignore' });
  const url = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(`${url}/api/health`)).ok) break; } catch { /* starting */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const pb = new PocketBase({ url, identity: 'svc@local.invalid', password: 'a-long-test-password' });
  await pb.authenticate();
  return { pb, stop: () => { child.kill(); fs.rmSync(dir, { recursive: true, force: true }); } };
}

test('PbStore keeps a synced world in a real PocketBase and the server reads it back', { skip: !BIN }, async () => {
  const { pb, stop } = await startPocketBase();
  try {
    const store = new PbStore(pb);
    await store.init();
    const world = new FakeWorld();
    world.addChat(ONE_TO_ONE, [raw(at(1), '<p>first</p>'), raw(at(2), '<p>Budget plan for Q4</p>')]);
    world.addChat(GROUP, [raw(at(10), '<p>group hello</p>')], { topic: 'Project room' });
    const sync = createSync({ store, teams: world.teams(), media: fakeMedia(), auth: fakeAuth(), now: () => at(60), sleep: async () => {}, random: () => 0.5 });
    await sync.pollOnce();
    while (await sync.backfillStep()) { /* copy history */ }
    const again = new PbStore(pb);
    await again.init();
    assert.equal(again.allChats().length, 2);
    assert.equal((await again.listMessages(ONE_TO_ONE)).messages.length, 2);
    assert.equal((await again.search('budget plan')).length, 1);
    assert.equal((await again.search("it's \\ 'quoted'")).length, 0, 'quotes in a search cannot break the filter');
    const second = await again.upsertMessages(ONE_TO_ONE, [{ ...(await again.getMessage(ONE_TO_ONE, String(at(2)))), edited_at: at(3), version: '99', content: '<p>Budget plan v2</p>' }]);
    assert.equal(second.updated.length, 1);
    await again.setState('probe', { a: 1 });
    assert.deepEqual(new PbStore(pb).getState('probe'), null);
    const third = new PbStore(pb); await third.init();
    assert.deepEqual(third.getState('probe'), { a: 1 });
    const server = createServer({ token: 't'.repeat(40), store: again, sync, teams: world.teams(), auth: fakeAuth(), media: fakeMedia() });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const base = `http://127.0.0.1:${server.address().port}`;
      const response = await fetch(`${base}/chats`, { headers: { Authorization: `Bearer ${'t'.repeat(40)}` } });
      assert.equal(response.status, 200);
      assert.equal((await response.json()).chats.length, 2);
    } finally { server.close(); }
  } finally { stop(); }
});

test('calendar migration stores same Graph event id independently in each subscribed calendar', { skip: !BIN }, async () => {
  const { pb, stop } = await startPocketBase();
  try {
    const rawEvent = (id, subject) => ({ id, subject, start: { dateTime: '2026-10-13T09:00:00Z' }, end: { dateTime: '2026-10-13T10:00:00Z' } });
    const metadata = [
      { id: 'default-graph', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
      { id: 'birthday-graph', name: 'Birthdays', canEdit: false },
      { id: 'canteen-graph', name: 'canteen', canEdit: false }
    ];
    const graph = { status: () => ({ state: 'ok', writable: true }), async request(_method, requestPath) {
      if (requestPath.startsWith('/me/calendars?')) return { value: metadata };
      const scoped = requestPath.match(/^\/me\/calendars\/([^/]+)\/calendarView\/delta/);
      const subject = scoped ? (scoped[1] === 'birthday-graph' ? 'Birthday' : 'Lunch') : 'Primary';
      return { value: [rawEvent('same-graph-id', subject)], '@odata.deltaLink': `https://graph.microsoft.com/v1.0${requestPath.split('?')[0]}?cursor=next` };
    } };
    const calendar = createCalendar({ pb, graph, now: () => T0 }); await calendar.init(); await calendar.sync();
    assert.equal(calendar.list({ start: '2026-10-13T00:00:00Z', end: '2026-10-14T00:00:00Z' }).length, 3);
    const rows = await pb.listAll('calendar_events');
    assert.equal(rows.length, 3);
    assert.deepEqual(new Set(rows.map(row => row.calendar_id)), new Set(['default', 'birthday-graph', 'canteen-graph']));
    const reloaded = createCalendar({ pb, graph: { status: () => ({ state: 'signed-out', writable: false }), request: async () => { throw new GraphError('Signed out', 401); } }, now: () => T0 });
    await reloaded.init();
    assert.equal(reloaded.list({ start: '2026-10-13T00:00:00Z', end: '2026-10-14T00:00:00Z' }).length, 3);
    assert.equal(reloaded.status().calendars.find(item => item.id === 'birthday-graph').canEdit, false);
  } finally { stop(); }
});
