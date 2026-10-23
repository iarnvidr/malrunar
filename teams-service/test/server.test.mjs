import test from 'node:test';
import assert from 'node:assert/strict';
import { MemoryStore } from '../src/store.mjs';
import { createSync } from '../src/sync.mjs';
import { createServer } from '../src/server.mjs';
import { FakeWorld, ONE_TO_ONE, GROUP, raw, from, ME, fakeMedia } from '../dev/fakes.mjs';

const TOKEN = 'x'.repeat(48);
const T0 = Date.now() - 3600_000;
const at = minutes => T0 + minutes * 60000;

async function start({ files, calendar } = {}) {
  const world = new FakeWorld();
  world.addChat(ONE_TO_ONE, [raw(at(1), '<p>hi <b>there</b></p><script>bad()</script>'), raw(at(2), 'mine', { messagetype: 'Text', from: from(ME), imdisplayname: 'Alex Example' })]);
  world.addChat(GROUP, [raw(at(5), '<p>group</p>')], { topic: 'Room' });
  const store = new MemoryStore();
  const authState = { haveTokens: true, state: 'ok' };
  const auth = { ensure: async () => authState.state !== 'signed-out', refused: async () => true, status: () => ({ ...authState }), snapshot: () => ({ ok: true }) };
  const teams = world.teams();
  const sync = createSync({ store, teams, media: fakeMedia(), auth, sleep: async () => {} });
  await sync.pollOnce();
  const server = createServer({ token: TOKEN, store, sync, teams, auth, media: fakeMedia(), files, calendar, myName: () => 'Alex Example' });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, path, body, token = TOKEN) => {
    const response = await fetch(base + path, { method, headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json().catch(() => null) };
  };
  return { world, store, authState, call, base, close: () => server.close() };
}

const enc = encodeURIComponent;

test('calendar response route authenticates and forwards the event key and response', async () => {
  const calls = [];
  const { call, close } = await start({ calendar: { respond: async (key, body) => { calls.push({ key, body }); return null; } } });
  try {
    const path = `/calendar/events/${enc('invite/id')}/respond`;
    assert.equal((await call('POST', path, { response: 'decline' }, null)).status, 401);
    const result = await call('POST', path, { response: 'decline' });
    assert.equal(result.status, 200); assert.deepEqual(result.body, { ok: true, event: null });
    assert.deepEqual(calls, [{ key: 'invite/id', body: { response: 'decline' } }]);
  } finally { close(); }
});

test('every route but health needs the bearer token', async () => {
  const { call, close } = await start();
  try {
    assert.equal((await call('GET', '/health', undefined, null)).status, 200);
    for (const [method, path] of [['GET', '/status'], ['GET', '/chats'], ['POST', `/chats/${enc(ONE_TO_ONE)}/send`], ['GET', '/search?q=hello']]) {
      assert.equal((await call(method, path, method === 'POST' ? { text: 'x' } : undefined, null)).status, 401, path);
      assert.equal((await call(method, path, method === 'POST' ? { text: 'x' } : undefined, 'wrong'.repeat(10))).status, 401, path);
    }
  } finally { close(); }
});

test('reading lists chats with unread counts, renders messages safely and searches the copy', async () => {
  const { call, close } = await start();
  try {
    const { body: list } = await call('GET', '/chats');
    assert.deepEqual(list.chats.map(chat => chat.title).sort(), ['Anna Example', 'Room']);
    assert.equal(list.totalMatches, 2);
    assert.equal(list.hasMore, false);
    const direct = list.chats.find(chat => chat.kind === 'oneOnOne');
    assert.equal(direct.unread, 0, 'never opened and the newest message is mine');
    const { body: page } = await call('GET', `/chats/${enc(ONE_TO_ONE)}/messages`);
    assert.equal(page.messages.length, 2);
    assert.doesNotMatch(JSON.stringify(page), /<script|bad\(\)/);
    assert.equal(page.messages[1].from, 'You');
    assert.equal(page.messages[1].mine, true);
    assert.match(page.messages[0].html, /<b>there<\/b>/);
    const { body: found } = await call('GET', '/search?q=THERE');
    assert.equal(found.results.length, 1);
    assert.equal(found.results[0].chatTitle, 'Anna Example');
    assert.equal((await call('GET', '/chats/nope/messages')).status, 404);
  } finally { close(); }
});

test('unread chat filtering happens before pagination and includes unread meetings in All', async () => {
  const { store, call, close } = await start();
  try {
    // A read chat is newer than both unread results. Filtering after the page
    // limit would return an empty first page and miss the unread chat below it.
    store.getChat(ONE_TO_ONE).last_at = at(30);
    store.getChat(ONE_TO_ONE).unread = 0;
    store.getChat(GROUP).last_at = at(20);
    store.getChat(GROUP).unread = 2;
    await store.upsertChat('19:unread-meeting@thread.v2', {
      kind: 'meeting', title: 'Unread meeting', preview: 'Meeting preview', unread: 1,
      last_at: at(10), hidden: false
    });

    const { body: newest } = await call('GET', '/chats?limit=1');
    assert.equal(newest.chats[0].id, ONE_TO_ONE);
    assert.equal(newest.chats[0].unread, 0);

    const { body: first } = await call('GET', '/chats?unread=1&limit=1&offset=0');
    assert.equal(first.chats.length, 1);
    assert.equal(first.chats[0].id, GROUP);
    assert.equal(first.totalMatches, 2);
    assert.equal(first.offset, 0);
    assert.equal(first.limit, 1);
    assert.equal(first.hasMore, true);

    const { body: second } = await call('GET', '/chats?unread=1&limit=1&offset=1');
    assert.equal(second.chats.length, 1);
    assert.equal(second.chats[0].kind, 'meeting');
    assert.equal(second.totalMatches, 2);
    assert.equal(second.offset, 1);
    assert.equal(second.hasMore, false);

    const { body: chatOnly } = await call('GET', '/chats?kind=chats&unread=1&limit=1');
    assert.deepEqual(chatOnly.chats.map(chat => chat.kind), ['group']);
    assert.equal(chatOnly.totalMatches, 1);
    assert.equal(chatOnly.hasMore, false);
  } finally { close(); }
});

test('send, edit, react, mark read and delete go live and update the copy', async () => {
  const { world, store, call, close } = await start();
  try {
    const sent = await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'teams-service test\nsecond line' });
    assert.equal(sent.status, 200);
    assert.equal(sent.body.message.mine, true);
    assert.match(sent.body.message.html, /teams-service test\nsecond line/);
    const id = sent.body.message.id;
    assert.ok(world.calls.some(item => item[0] === 'send' && item[2] === 'teams-service test\nsecond line'));

    const edited = await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${id}/edit`, { text: 'edited' });
    assert.equal(edited.status, 200);
    assert.ok(edited.body.message.edited > 0);
    assert.match(edited.body.message.html, /edited/);

    const reacted = await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${id}/reaction`, { key: 'like' });
    assert.deepEqual(reacted.body.message.reactions.map(reaction => [reaction.key, reaction.mine]), [['like', true]]);
    const removed = await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${id}/reaction`, { key: 'like', on: false });
    assert.deepEqual(removed.body.message.reactions, []);

    const read = await call('POST', `/chats/${enc(ONE_TO_ONE)}/read`, {});
    assert.equal(read.status, 200);
    assert.equal(read.body.unread, 0);
    assert.ok(world.calls.some(item => item[0] === 'markRead'));

    const deleted = await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${id}/delete`, {});
    assert.equal(deleted.body.message.deleted, true);
    assert.equal((await store.getMessage(ONE_TO_ONE, id)).deleted, true);
  } finally { close(); }
});

test('only own messages can be edited or deleted; bad input never reaches Teams', async () => {
  const { world, call, close } = await start();
  try {
    const theirs = String(at(1));
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${theirs}/edit`, { text: 'x' })).status, 403);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${theirs}/delete`, {})).status, 403);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: '   ' })).status, 400);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'x'.repeat(30000) })).status, 400);
    assert.equal((await call('POST', `/chats/${enc('19:unknown@thread.v2')}/send`, { text: 'hi' })).status, 404);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/abc/edit`, { text: 'x' })).status, 400);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${theirs}/reaction`, { key: 'bad key!' })).status, 400);
    assert.equal(world.calls.filter(item => ['send', 'edit', 'remove', 'react'].includes(item[0])).length, 0);
  } finally { close(); }
});

test('signed out: reads still work and every write fails visibly without being queued', async () => {
  const { world, authState, call, close } = await start();
  try {
    authState.state = 'signed-out';
    authState.haveTokens = false;
    world.signedOut = true;
    assert.equal((await call('GET', '/chats')).status, 200);
    assert.equal((await call('GET', `/chats/${enc(ONE_TO_ONE)}/messages`)).body.messages.length, 2);
    const sent = await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'hello' });
    assert.equal(sent.status, 503);
    assert.equal(sent.body.signedOut, true);
    for (const path of [`/chats/${enc(ONE_TO_ONE)}/messages/${at(2)}/edit`, `/chats/${enc(ONE_TO_ONE)}/messages/${at(2)}/delete`, `/chats/${enc(ONE_TO_ONE)}/messages/${at(2)}/reaction`, `/chats/${enc(ONE_TO_ONE)}/read`]) {
      const result = await call('POST', path, { text: 'x', key: 'like' });
      assert.equal(result.status, 503, path);
    }
    world.signedOut = false;
    authState.state = 'ok';
    authState.haveTokens = true;
    assert.equal(world.calls.filter(item => ['send', 'edit', 'remove', 'react', 'markRead'].includes(item[0])).length, 0, 'nothing was queued and replayed');
    const status = await call('GET', '/status');
    assert.equal(status.status, 200);
  } finally { close(); }
});

test('malformed bodies and unknown routes get clean errors', async () => {
  const { call, close, base } = await start();
  try {
    assert.equal((await call('GET', '/nope')).status, 404);
    const headers = { Authorization: `Bearer ${TOKEN}` };
    assert.equal((await fetch(`${base}/chats/${enc(ONE_TO_ONE)}/send`, { method: 'POST', headers, body: '{not json' })).status, 400);
    assert.equal((await fetch(`${base}/chats/${enc(ONE_TO_ONE)}/send`, { method: 'POST', headers, body: '[1]' })).status, 400);
    assert.equal((await fetch(`${base}/chats/%E0%A4%A/messages`, { headers })).status, 400);
  } finally { close(); }
});

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');

test('an image is shared as a Teams file and sent with standard file metadata', async () => {
  const uploadedFiles = [];
  const files = { upload: async data => {
    uploadedFiles.push(data);
    return { id: 'image-file', url: 'https://contoso.sharepoint.com/image-file', name: data.name, type: 'png' };
  } };
  const { world, base, call, close } = await start({ files });
  try {
    const upload = (type, bytes, name = 'shot one.png') => fetch(`${base}/chats/${enc(ONE_TO_ONE)}/upload`, {
      method: 'POST', headers: { Authorization: `Bearer ${TOKEN}`, 'X-File-Type': type, 'X-File-Name': enc(name) }, body: bytes
    });
    assert.equal((await upload('image/png', Buffer.alloc(0))).status, 400);
    assert.equal((await upload('image/png', Buffer.alloc(10 * 1024 * 1024 + 1))).status, 413);
    const response = await upload('image/png', PNG);
    assert.equal(response.status, 200);
    const uploaded = await response.json();
    assert.equal(uploaded.name, 'shot one.png');
    assert.deepEqual([uploaded.width, uploaded.height], [1, 1]);
    assert.equal(uploadedFiles.length, 1);
    assert.deepEqual(uploadedFiles[0].memberMris, ['8:orgid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa']);

    assert.equal((await call('POST', `/chats/${enc(GROUP)}/send`, { text: 'x', attachments: [uploaded.uploadId] })).status, 400, 'an upload belongs to its chat');
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'x', attachments: ['nope'] })).status, 400);
    const sent = await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'look <here>\nnow', attachments: [uploaded.uploadId] });
    assert.equal(sent.status, 200);
    const call2 = world.calls.find(item => item[0] === 'send' && item[3] === 'html');
    assert.match(call2[2], /^<p>look &lt;here&gt;<br>now<\/p><p><a href="https:\/\/contoso\.sharepoint\.com\/image-file">shot one\.png<\/a><\/p>$/);
    assert.deepEqual(call2[4].map(file => ({ id: file.id, fileName: file.name, fileType: file.type, objectUrl: file.url })), [
      { id: 'image-file', fileName: 'shot one.png', fileType: 'png', objectUrl: 'https://contoso.sharepoint.com/image-file' }
    ]);
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: 'again', attachments: [uploaded.uploadId] })).status, 400, 'an upload is used once');

    const imageOnly = await (await upload('image/png', PNG)).json();
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { attachments: [imageOnly.uploadId] })).status, 200, 'an image alone is enough');
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { text: '  ' })).status, 400);
    const messages = (await call('GET', `/chats/${enc(ONE_TO_ONE)}/messages`)).body.messages;
    const withImage = messages.find(message => /look/.test(message.html));
    const edit = await call('POST', `/chats/${enc(ONE_TO_ONE)}/messages/${withImage.id}/edit`, { text: 'new' });
    assert.equal(edit.status, 409, 'editing would drop the image');
  } finally { close(); }
});

test('uploading while signed out says so and sends nothing', async () => {
  const files = { upload: async () => { throw new Error('upload should not run while signed out'); } };
  const { world, base, authState, close } = await start({ files });
  try {
    authState.state = 'signed-out'; authState.haveTokens = false;
    const response = await fetch(`${base}/chats/${enc(ONE_TO_ONE)}/upload`, { method: 'POST', headers: { Authorization: `Bearer ${TOKEN}`, 'X-File-Type': 'image/png' }, body: PNG });
    assert.equal(response.status, 503);
    assert.equal((await response.json()).signedOut, true);
    assert.equal(world.calls.filter(item => item[0] === 'send').length, 0);
  } finally { close(); }
});

test('a document upload belongs to one chat and is shared only with its members', async () => {
  const uploaded = [];
  const files = { upload: async data => { uploaded.push(data); return { id: 'file', url: 'https://contoso.sharepoint.com/file', name: data.name, type: 'pdf' }; } };
  const { base, call, close } = await start({ files });
  try {
    const response = await fetch(`${base}/chats/${enc(ONE_TO_ONE)}/upload`, { method: 'POST',
      headers: { Authorization: `Bearer ${TOKEN}`, 'X-File-Type': 'application/pdf', 'X-File-Name': 'report.pdf' }, body: Buffer.from('report') });
    assert.equal(response.status, 200);
    assert.deepEqual(uploaded[0].memberMris, ['8:orgid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa']);
    const attachment = await response.json();
    assert.equal((await call('POST', `/chats/${enc(ONE_TO_ONE)}/send`, { attachments: [attachment.uploadId] })).status, 200);
  } finally { close(); }
});
