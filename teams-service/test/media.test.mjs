import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createMediaCache } from '../src/media.mjs';
import { createFileClient } from '../src/files.mjs';
import { mediaKey } from '../src/messages.mjs';
import { MemoryStore } from '../src/store.mjs';

const IMAGE = 'https://eu-api.asm.skype.com/v1/objects/image/views/imgo';
const FILE = 'https://contoso.sharepoint.com/sites/docs/report.pdf';

test('an inaccessible image is delayed so later attachments still download', async () => {
  const store = new MemoryStore(), dir = fs.mkdtempSync(path.join(os.tmpdir(), 'malrunar-media-'));
  const media = createMediaCache({ dir, store, skypeToken: () => 'token', fetchImpl: async url => new Response(url === IMAGE ? '' : 'image', { status: url === IMAGE ? 403 : 200, headers: { 'Content-Type': 'image/png' } }) });
  try {
    await media.register('chat', 'message', [IMAGE, IMAGE + '?other'].map(url => ({ url, key: mediaKey(url) })));
    assert.equal((await media.processPending()).auth, true);
    assert.equal((await store.getMedia(mediaKey(IMAGE))).status, 'failed');
    assert.equal((await media.processPending()).ok, 1);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('shared files and images remain readable after access expires and a service restart', async () => {
  const store = new MemoryStore(), dir = fs.mkdtempSync(path.join(os.tmpdir(), 'malrunar-media-'));
  try {
    const media = createMediaCache({ dir, store, skypeToken: () => 'token',
      fetchImpl: async () => new Response('picture', { headers: { 'Content-Type': 'image/png' } }),
      fileDownload: async () => new Response('cached pdf', { headers: { 'Content-Type': 'application/pdf' } }) });
    await media.register('chat', 'message', [IMAGE, FILE].map(url => ({ url, key: mediaKey(url) })));
    assert.equal((await media.processPending()).ok, 2);
    const offline = createMediaCache({ dir, store, skypeToken: () => null, fetchImpl: () => { throw new Error('offline'); } });
    const file = await offline.read(mediaKey(FILE));
    assert.equal(fs.readFileSync(file.file, 'utf8'), 'cached pdf');
    assert.equal(file.attachment, true);
    assert.equal((await offline.read(mediaKey(IMAGE))).attachment, false);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('file content downloads never forward the Graph bearer to the content host', async () => {
  const calls = [];
  const client = createFileClient({ token: async () => 'private-token', fetchImpl: async (url, options) => {
    calls.push([String(url), options]);
    return String(url).startsWith('https://graph.microsoft.com') ? Response.json({ size: 2, '@microsoft.graph.downloadUrl': 'https://contoso.sharepoint.com/content?signature=private' }) : new Response('ok');
  } });
  assert.equal(await (await client.download(FILE)).text(), 'ok');
  assert.match(calls[0][0], /\/shares\/u!/);
  assert.equal(calls[0][1].headers.Authorization, 'Bearer private-token');
  assert.equal(calls[1][1].headers, undefined);
  assert.equal(calls[1][1].redirect, 'error');
  await assert.rejects(client.download('http://127.0.0.1/private'), /Unsupported/);
});

test('file upload gives read access only to specified chat members, without emails', async () => {
  const calls = [];
  const client = createFileClient({ token: async () => 'token', fetchImpl: async (url, options) => {
    calls.push([url, options]);
    if (options.method === 'PUT') return Response.json({ id: 'item', webUrl: FILE });
    if (String(url).endsWith('/invite')) return Response.json({ value: [{ id: 'permission' }] });
    return Response.json({ id: 'folder' });
  } });
  const uploaded = await client.upload({ bytes: Buffer.from('report'), name: 'report.pdf', type: 'application/pdf', memberMris: ['8:orgid:11111111-1111-1111-1111-111111111111'] });
  assert.equal(uploaded.url, FILE);
  const invite = JSON.parse(calls.find(([url]) => url.endsWith('/invite'))[1].body);
  assert.deepEqual(invite.recipients, [{ objectId: '11111111-1111-1111-1111-111111111111' }]);
  assert.equal(invite.sendInvitation, false);
  assert.deepEqual(invite.roles, ['read']);
});
