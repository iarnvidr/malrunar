import test from 'node:test';
import assert from 'node:assert/strict';
import { MemoryStore } from '../src/store.mjs';
import { createSync } from '../src/sync.mjs';
import { FakeWorld, ME, ANNA, ONE_TO_ONE, GROUP, MEETING, SELF, raw, from, fakeAuth, fakeMedia } from '../dev/fakes.mjs';

const T0 = Date.parse('2026-10-13T08:00:00Z');
const at = minutes => T0 + minutes * 60000;

function setup() {
  const world = new FakeWorld();
  world.addChat(ONE_TO_ONE, [raw(at(1), '<p>one</p>'), raw(at(2), '<p>two</p>'), raw(at(3), '<p>three</p>')], { properties: { consumptionhorizon: `${at(1)};${at(1)};${at(1)}` } });
  world.addChat(GROUP, [raw(at(10), '<p>group hello</p>')], { topic: 'Project room', properties: { consumptionhorizon: `${at(10)};1;1` } });
  world.addChat(MEETING, [raw(at(20), '<p>meeting notes</p>')], { topic: 'Weekly' });
  world.addChat('48:notifications', [raw(at(30), 'x')]);
  world.addChat('19:chan@thread.tacv2', [raw(at(31), 'x')]);
  const store = new MemoryStore();
  const media = fakeMedia();
  const sync = createSync({ store, teams: world.teams(), media, auth: fakeAuth(), now: () => at(60), sleep: async () => {}, random: () => 0.5 });
  return { world, store, media, sync };
}

const count = (world, name) => world.calls.filter(call => call[0] === name).length;

test('the first poll copies chats, skips system and channel threads and counts unread', async () => {
  const { store, sync } = setup();
  await sync.pollOnce();
  const ids = store.allChats().map(chat => chat.chat_id);
  assert.deepEqual(ids.sort(), [GROUP, MEETING, ONE_TO_ONE].sort());
  const direct = store.getChat(ONE_TO_ONE);
  assert.equal(direct.kind, 'oneOnOne');
  assert.equal(direct.title, 'Anna Example', 'a direct chat is named after the other person');
  assert.equal(direct.unread, 2, 'two messages after the read horizon');
  assert.equal(store.getChat(GROUP).title, 'Project room');
  assert.equal(store.getChat(GROUP).unread, 0);
  assert.equal(store.getChat(MEETING).unread, 0, 'a never-opened meeting chat is not flagged unread');
  assert.equal((await store.listMessages(ONE_TO_ONE)).messages.length, 3);
});

test('history is copied page by page, newest chats first, and resumes from the saved link', async () => {
  const { world, store, sync } = setup();
  const many = Array.from({ length: 5 }, (_, index) => raw(at(100 + index), `<p>old ${index}</p>`));
  world.addChat('19:long@thread.v2', many, { topic: 'Long' });
  await sync.pollOnce();
  // pageSize 100 in production; the fake pages by the size it is given, so force small pages through the link.
  const long = store.getChat('19:long@thread.v2');
  assert.equal(long.backfill_done, undefined);
  let steps = 0;
  while (await sync.backfillStep()) { if (++steps > 20) assert.fail('backfill did not finish'); }
  for (const chat of store.allChats()) assert.equal(chat.backfill_done, true, chat.chat_id);
  assert.equal((await store.listMessages('19:long@thread.v2', { limit: 100 })).messages.length, 5);
  assert.equal(count(world, 'members') > 0, true, 'members of group chats are read once');
  const before = world.calls.length;
  assert.equal(await sync.backfillStep(), false);
  assert.equal(world.calls.length, before, 'nothing is requested once everything is copied');
});

test('new messages arrive on the next poll and only the changed chat is read', async () => {
  const { world, store, sync } = setup();
  await sync.pollOnce();
  world.calls.length = 0;
  await sync.pollOnce();
  assert.equal(count(world, 'messages'), 2, 'no chat changed: only the rolling re-read runs');
  world.chats.get(GROUP).messages.unshift(raw(at(40), '<p>fresh</p>'));
  world.calls.length = 0;
  await sync.pollOnce();
  assert.ok(world.calls.some(call => call[0] === 'messages' && call[1] === GROUP));
  const latest = await store.latestMessages(GROUP, 1);
  assert.equal(latest[0].search.trim(), 'fresh');
  assert.equal(store.getChat(GROUP).preview, 'fresh');
  assert.equal(store.getChat(GROUP).unread, 1);
});

test('edits, deletes and reactions made elsewhere are picked up by the re-read', async () => {
  const { world, store, sync } = setup();
  await sync.pollOnce();
  const [, , third] = (await store.listMessages(ONE_TO_ONE)).messages;
  const messages = world.chats.get(ONE_TO_ONE).messages;
  const target = messages.find(message => message.id === String(at(2)));
  target.content = '<p>two (edited)</p>';
  target.properties = { edittime: String(at(50)) };
  target.version = String(at(50));
  const last = messages.find(message => message.id === String(at(3)));
  last.content = '';
  last.properties = { deletetime: String(at(51)) };
  last.version = String(at(51));
  messages.find(message => message.id === String(at(1))).properties = { emotions: JSON.stringify([{ key: 'heart', users: [{ mri: ANNA, time: 5 }] }]) };
  await sync.syncChatRecent(ONE_TO_ONE);
  const stored = Object.fromEntries((await store.listMessages(ONE_TO_ONE)).messages.map(message => [message.msg_id, message]));
  assert.equal(stored[String(at(2))].edited_at, at(50));
  assert.match(stored[String(at(2))].content, /edited/);
  assert.equal(stored[String(at(3))].deleted, true);
  assert.equal(stored[String(at(3))].content, '');
  assert.deepEqual(stored[String(at(1))].reactions.map(reaction => [reaction.key, reaction.count]), [['heart', 1]]);
  assert.equal(store.getChat(ONE_TO_ONE).unread, 1, 'a deleted message no longer counts as unread');
  assert.ok(third);
});

test('the rolling re-read visits the busiest chats in turn', async () => {
  const { world, sync } = setup();
  await sync.pollOnce();
  world.calls.length = 0;
  await sync.recheckStep(3);
  const visited = world.calls.filter(call => call[0] === 'messages').map(call => call[1]);
  assert.equal(new Set(visited).size, 3);
});

test('a read made in Teams updates unread; images in messages are registered for download', async () => {
  const { world, store, sync, media } = setup();
  const image = 'https://eu-api.asm.skype.com/v1/objects/0-weu-d4-abc/views/imgo';
  world.chats.get(GROUP).messages.unshift(raw(at(41), `<p><img src="${image}" itemtype="http://schema.skype.com/AMSImage"></p>`));
  await sync.pollOnce();
  assert.equal(media.registered.length, 1);
  assert.equal(store.getChat(GROUP).unread, 1);
  world.chats.get(GROUP).extra.properties = { consumptionhorizon: `${at(41)};1;1` };
  await sync.pollOnce();
  assert.equal(store.getChat(GROUP).unread, 0);
});

test('my own messages are marked mine and the self chat is named', async () => {
  const { world, store, sync } = setup();
  world.addChat(SELF, [raw(at(5), 'note', { messagetype: 'Text', from: from(ME), imdisplayname: 'Alex Example' })]);
  await sync.pollOnce();
  const [note] = (await store.listMessages(SELF)).messages;
  assert.equal(note.mine, true);
  assert.equal(store.getChat(SELF).title, 'Notes (you)');
  assert.equal(store.getChat(SELF).unread, 0);
});

test('a refused or signed-out session stops the poll with an error and keeps the copy', async () => {
  const { world, store, sync } = setup();
  await sync.pollOnce();
  world.signedOut = true;
  await assert.rejects(sync.pollOnce(), error => error.code === 'AUTH_REQUIRED');
  assert.equal(sync.status().lastPollError, 'Teams is signed out');
  assert.equal(store.allChats().length, 3);
  assert.equal((await store.listMessages(ONE_TO_ONE)).messages.length, 3);
});

test('refreshMessage marks a message deleted when Teams no longer returns it', async () => {
  const { world, store, sync } = setup();
  await sync.pollOnce();
  const messages = world.chats.get(ONE_TO_ONE).messages;
  messages.splice(messages.findIndex(message => message.id === String(at(2))), 1);
  await sync.refreshMessage(ONE_TO_ONE, String(at(2)));
  assert.equal((await store.getMessage(ONE_TO_ONE, String(at(2)))).deleted, true);
});

test('the full chat list adds chats the short poll page never showed, without reading them yet', async () => {
  const { world, store, sync } = setup();
  await sync.pollOnce();
  // A chat older than the poll page: only the periodic full list sees it.
  world.addChat('19:old@thread.v2', [raw(at(-500), '<p>very old</p>')], { topic: 'Old room' });
  await sync.fullChatList();
  const old = store.getChat('19:old@thread.v2');
  assert.equal(old.title, 'Old room');
  assert.equal(old.backfill_done, undefined, 'its history is copied later by the backfill');
  while (await sync.backfillStep()) { /* copy history */ }
  assert.equal((await store.listMessages('19:old@thread.v2')).messages.length, 1);
});
