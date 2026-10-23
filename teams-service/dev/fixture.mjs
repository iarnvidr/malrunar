// A throwaway Teams service with made-up chats for working on the web view
// without Microsoft: the real server, sync and store code, over an in-memory
// store and a scripted Teams world.
//
//   node dev/fixture.mjs            # service on 127.0.0.1:28390, control on 28399
//   TOKEN=dev-token-dev-token-dev-token-dev-token-0000
//
// Control (no auth, loopback):
//   POST :28399/signout  POST :28399/signin           sign-in state
//   POST :28399/incoming?chat=<id>&text=...           a new message from someone else
// UPLOAD_DELAY_MS=1500 makes image uploads slow, to see the send button wait for them.
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { MemoryStore } from '../src/store.mjs';
import { createSync } from '../src/sync.mjs';
import { createServer } from '../src/server.mjs';
import { mediaKey } from '../src/messages.mjs';
import { imageSize } from '../src/media.mjs';
import { FakeWorld, ME, ANNA, raw, from } from './fakes.mjs';
import { createCalendarFixture } from './calendar-fixture.mjs';

export const TOKEN = process.env.TOKEN || 'dev-token-dev-token-dev-token-dev-token-0000';
const PORT = Number(process.env.PORT || 28390);
const CONTROL_PORT = Number(process.env.CONTROL_PORT || 28399);

function png(width, height, [r, g, b]) {
  const rows = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const o = y * (width * 3 + 1);
    rows[o] = 0;
    const shade = (x + y) % 40 < 20 ? 1 : 0.8;
    rows[o + 1 + x * 3] = r * shade; rows[o + 2 + x * 3] = g * shade; rows[o + 3 + x * 3] = b * shade;
  }
  const crcTable = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
  const crc = buffer => { let c = 0xffffffff; for (const byte of buffer) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
  const chunk = (type, data) => { const out = Buffer.alloc(12 + data.length); out.writeUInt32BE(data.length, 0); out.write(type, 4); data.copy(out, 8); out.writeUInt32BE(crc(Buffer.concat([Buffer.from(type), data])), 8 + data.length); return out; };
  const header = Buffer.alloc(13); header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0))]);
}

const NAMES = ['Anna Example', 'Blake Sample', 'Casey Demo-Placeholder', 'Drew Fixture', 'Emery Test', 'Finley Longname-Hyphenated-Example', 'Gray Sample', 'Harper Demo', 'Indigo Placeholder', 'Zoë Fixture'];
const person = index => { const mri = index === 0 ? ANNA : `8:orgid:${String(index).padStart(8, '0')}-0000-0000-0000-000000000000`; return { mri, name: NAMES[index % NAMES.length] + (index >= NAMES.length ? ` ${Math.floor(index / NAMES.length) + 1}` : '') }; };

const IMAGE_URL = id => `https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d${id}-fixture/views/imgo`;
const imageFiles = new Map();
const imageDims = new Map();
const imageTypes = new Map();
const fileKeys = new Set();
const UPLOAD_DELAY_MS = Number(process.env.UPLOAD_DELAY_MS || 0);

export async function build() {
  const world = new FakeWorld();
  const now = Date.now();
  const minutes = n => now - n * 60000;
  let serial = 0;
  const id = ts => String(Math.floor(ts) + (serial++));
  const say = (who, ts, content, extra = {}) => raw(id(ts), content, { from: from(who.mri), imdisplayname: who.name, ...extra });

  const people = Array.from({ length: 24 }, (_, index) => person(index));
  for (const entry of people) world.names.set(entry.mri, entry.name);
  world.names.set(ME, 'Alex Example');
  const mine = (ts, content, extra = {}) => say({ mri: ME, name: 'Alex Example' }, ts, content, extra);

  // One-to-one chats, newest first, a third of them unread.
  people.slice(0, 14).forEach((entry, index) => {
    const chatId = `19:${entry.mri.slice(8)}_${ME.slice(8)}@unq.gbl.spaces`;
    const messages = [];
    const count = index === 0 ? 160 : 6 + (index * 7) % 30;
    for (let k = 0; k < count; k++) {
      const ts = minutes(index * 90 + (count - k) * 11);
      if (k % 3 === 2) messages.push(mine(ts, k % 7 === 2 ? `<p>Sounds good, ${k}</p>` : `Thanks, noted (${k})`, k % 7 === 2 ? {} : { messagetype: 'Text' }));
      else messages.push(say(entry, ts, k % 5 === 0 ? `<p>See <a href="https://example.org/doc/${k}">the document</a> and tell me if anything is missing. This is a somewhat longer message, so that it takes more than one line in the thread when the window is narrow.</p>` : `<p>Hi Alex, question number ${k}: can we have a short meeting tomorrow about the budget?</p>`));
    }
    const read = index % 3 === 0 ? messages[messages.length - 1].originalarrivaltime : messages[Math.max(0, messages.length - 4)].originalarrivaltime;
    world.addChat(chatId, messages, { properties: { consumptionhorizon: `${Date.parse(read)};${Date.parse(read)};${Date.parse(read)}` } });
    if (index === 0) {
      // Rich content in the first chat: image, reaction, edited, deleted, quote and mention, long unbroken text.
      const key = mediaKey(IMAGE_URL(1)); imageFiles.set(key, png(640, 360, [60, 120, 200]));
      const key2 = mediaKey(IMAGE_URL(2)); imageFiles.set(key2, png(300, 700, [200, 110, 60]));
      messages.unshift(say(entry, minutes(30), `<p>Here is the screenshot</p><p><img src="${IMAGE_URL(1)}" alt="screenshot" width="640" height="360" itemscope="" itemtype="http://schema.skype.com/AMSImage"></p>`));
      messages.unshift(say(entry, minutes(29), `<p><img src="${IMAGE_URL(2)}" alt="tall" width="300" height="700" itemscope="" itemtype="http://schema.skype.com/AMSImage"></p>`));
      messages.unshift(say(entry, minutes(28), '<p>Deadline is <b>Friday</b>, <i>please</i> confirm.</p><ul><li>one</li><li>two</li></ul>', { properties: { emotions: JSON.stringify([{ key: 'like', users: [{ mri: ME, time: 1 }, { mri: entry.mri, time: 2 }] }, { key: 'heart', users: [{ mri: entry.mri, time: 3 }] }]) } }));
      messages.unshift(mine(minutes(27), 'Edited by me', { messagetype: 'Text', properties: { edittime: String(minutes(26)) } }));
      messages.unshift(say(entry, minutes(26), '', { properties: { deletetime: String(minutes(25)) } }));
      messages.unshift(say(entry, minutes(24), `<blockquote itemscope="" itemtype="http://schema.skype.com/Reply"><strong>Alex</strong><p>Edited by me</p></blockquote><p>Reply to your edit with <span itemscope="" itemtype="http://schema.skype.com/Mention">@Alex</span> mentioned.</p>`));
      messages.unshift(say(entry, minutes(23), `<p>${'Superlongunbrokenwordwithoutanyspaces'.repeat(8)} https://example.org/${'very-long-path-segment/'.repeat(8)}</p>`));
      messages.unshift(say(entry, minutes(22), '<p>See attachment</p>', { properties: { files: JSON.stringify([{ id: 'f1', fileName: 'Budget 2027 — final (v3) with a rather long file name.xlsx', fileType: 'xlsx', objectUrl: 'https://contoso.sharepoint.com/budget.xlsx' }]) } }));
      world.chats.get(chatId).messages = messages.sort((a, b) => Number(b.id) - Number(a.id));
    }
  });

  const groups = [['Summer camp 2027 — planning and logistics for all volunteers', [1, 2, 3, 4]], ['Leadership', [5, 6]], ['Volunteers North', [7, 8, 9, 10, 11, 12]], ['Course: first aid for instructors (autumn)', [13, 14, 15]], ['', [16, 17, 18]], ['IT', [19, 20]]];
  groups.forEach(([topic, members], index) => {
    const chatId = `19:group${index}@thread.v2`;
    const messages = Array.from({ length: 12 + index * 9 }, (_, k) => say(people[members[k % members.length]], minutes(40 + index * 180 + (60 - k) * 7), `<p>Message ${k} in the group: remember the deadline and the minutes.</p>`));
    world.addChat(chatId, messages, { topic, properties: { consumptionhorizon: index % 2 ? '1;1;1' : `${Date.now()};1;1` } });
    world.members.set(chatId, [{ mri: ME, role: 'Admin' }, ...members.map(m => ({ mri: people[m].mri, role: 'User' }))]);
  });
  for (let index = 0; index < 5; index++) {
    world.addChat(`19:meeting_fixture${index}@thread.v2`, [say(people[index + 1], minutes(300 + index * 400), `<p>Meeting note ${index}</p>`)], { topic: `Weekly status meeting — team ${index + 1}` });
  }
  world.addChat('48:notes', [mine(minutes(900), 'Remember to call the accountant', { messagetype: 'Text' })]);
  // Plenty of chats to scroll in the list.
  for (let index = 14; index < 24; index++) {
    const entry = people[index];
    world.addChat(`19:${entry.mri.slice(8)}_${ME.slice(8)}@unq.gbl.spaces`, [say(entry, minutes(2000 + index * 100), `<p>Old conversation with ${entry.name}</p>`)]);
  }

  const store = new MemoryStore();
  const authState = { haveTokens: true, state: 'ok' };
  const auth = { ensure: async () => authState.state !== 'signed-out', refused: async () => authState.state !== 'signed-out', status: () => ({ ...authState, tokenExpiresAt: Date.now() + 3600_000, since: Date.now() }), snapshot: () => ({ ok: true }) };
  const teams = world.teams();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'malrunar-teams-fixture-'));
  const media = {
    register: async () => 0, processPending: async () => ({ ok: 0, failed: 0, auth: false }),
    read: async key => { const bytes = imageFiles.get(key); if (!bytes) return null; const file = path.join(dir, key); fs.writeFileSync(file, bytes); return { file, contentType: imageTypes.get(key) || 'image/png', size: bytes.length, attachment: fileKeys.has(key) }; }
  };
  const origMediaForKeys = store.mediaForKeys.bind(store);
  store.mediaForKeys = async keys => ({ ...(await origMediaForKeys(keys)), ...Object.fromEntries(keys.filter(key => imageFiles.has(key)).map(key => [key, { status: 'ok', ...(imageDims.get(key) || { width: key === mediaKey(IMAGE_URL(2)) ? 300 : 640, height: key === mediaKey(IMAGE_URL(2)) ? 700 : 360 }) }])) });
  // Images uploaded in the view use the same standard file metadata as other attachments.
  let uploadedFile = 0;
  const files = { upload: async options => {
    if (UPLOAD_DELAY_MS) await new Promise(resolve => setTimeout(resolve, UPLOAD_DELAY_MS));
    const id = `fixture-file-${++uploadedFile}`;
    const url = `https://contoso.sharepoint.com/${id}/${encodeURIComponent(options.name)}`;
    const key = mediaKey(url);
    imageFiles.set(key, options.bytes); imageDims.set(key, imageSize(options.bytes)); imageTypes.set(key, options.type || 'image/png'); fileKeys.add(key);
    return { id, url, name: options.name, type: options.name.split('.').pop()?.toLowerCase() || 'file', size: options.bytes.length };
  } };
  // Reactions add to or remove from the people already reacting, as Teams does.
  const editEmotions = (chatId, messageId, change) => {
    const message = world.chats.get(chatId).messages.find(item => item.id === messageId);
    const list = JSON.parse(message.properties?.emotions || '[]');
    change(list);
    message.properties = { ...message.properties, emotions: JSON.stringify(list.filter(entry => entry.users.length)) };
    message.version = String(Number(message.version) + 1);
  };
  teams.react = async (chatId, messageId, key) => editEmotions(chatId, messageId, list => {
    let entry = list.find(item => item.key === key);
    if (!entry) list.push(entry = { key, users: [] });
    if (!entry.users.some(user => user.mri === ME)) entry.users.push({ mri: ME, time: Date.now() });
  });
  teams.unreact = async (chatId, messageId, key) => editEmotions(chatId, messageId, list => {
    const entry = list.find(item => item.key === key);
    if (entry) entry.users = entry.users.filter(user => user.mri !== ME);
  });
  const sync = createSync({ store, teams, media, auth, sleep: async () => {}, random: () => 0.5 });
  await sync.pollOnce();
  while (await sync.backfillStep()) { /* copy all history */ }
  await sync.pollOnce();
  const calendar = await createCalendarFixture(authState);
  const server = createServer({ token: TOKEN, store, sync, teams, auth, media, files, calendar, myName: () => 'Alex Example' });
  return { world, store, sync, server, authState, people, say };
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  const ctx = await build();
  ctx.server.listen(PORT, '127.0.0.1', () => console.log(`fixture Teams service on 127.0.0.1:${PORT}`));
  http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname === '/signout') { ctx.authState.state = 'signed-out'; ctx.authState.haveTokens = false; }
    else if (url.pathname === '/signin') { ctx.authState.state = 'ok'; ctx.authState.haveTokens = true; }
    else if (url.pathname === '/incoming') {
      const chatId = url.searchParams.get('chat') || [...ctx.world.chats.keys()].find(key => key.includes('unq.gbl'));
      const who = ctx.people[0];
      ctx.world.chats.get(chatId).messages.unshift(ctx.say(who, Date.now(), `<p>${url.searchParams.get('text') || 'New message'}</p>`));
      await ctx.sync.pollOnce();
    }
    response.end('ok\n');
  }).listen(CONTROL_PORT, '127.0.0.1');
}
