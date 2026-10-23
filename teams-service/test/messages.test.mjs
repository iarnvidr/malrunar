import test from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeHtml, normalizeMessage, renderMessage, extractMedia, mediaKey, chatKind, oneOnOneMris, textToHtml, stripHtml, referencedMris, searchSnippet } from '../src/messages.mjs';

const ME = '8:orgid:11111111-1111-1111-1111-111111111111';
const IMG = 'https://eu-api.asm.skype.com/v1/objects/0-weu-d4-abc/views/imgo';

test('sanitizer drops scripts, event handlers and unsafe links but keeps the text', () => {
  const dirty = [
    '<p onclick="alert(1)">Hello <b style="x">world</b></p>',
    '<script>alert(1)</script>after',
    '<a href="javascript:alert(1)">bad</a> <a href=" JavaScript:alert(1)">bad2</a> <a href="data:text/html;base64,AAA">bad3</a>',
    '<a href="https://example.org/?a=1&b=2" onmouseover="x()">ok</a>',
    '<img src="x" onerror="alert(1)">',
    '<svg onload="alert(1)"><circle/></svg>',
    '<iframe src="https://evil.example"></iframe>',
    '<style>body{display:none}</style>',
    '<div><div><span>text</span>',
    '<!--><script>alert(2)</script>-->',
    '&lt;script&gt;alert(3)&lt;/script&gt;'
  ].join('\n');
  const clean = sanitizeHtml(dirty);
  assert.doesNotMatch(clean, /<script|onerror|onclick|onmouseover|javascript:|data:text|<svg|<iframe|<style|<circle/i);
  assert.match(clean, /Hello <b>world<\/b>/);
  assert.match(clean, /<a href="https:\/\/example.org\/\?a=1&amp;b=2" target="_blank" rel="noopener noreferrer nofollow">ok<\/a>/);
  assert.match(clean, /&lt;script&gt;alert\(3\)&lt;\/script&gt;/, 'escaped markup stays text');
  assert.match(clean, /bad bad2 bad3/, 'link text is kept');
  // Every tag in the output is from the allow-list.
  for (const tag of clean.matchAll(/<\/?([a-z0-9]+)/g)) assert.ok(['p', 'b', 'a', 'div', 'span', 'br'].includes(tag[1]), tag[1]);
  // Balanced.
  assert.equal((clean.match(/<div>/g) || []).length, (clean.match(/<\/div>/g) || []).length);
});

test('sanitizer never lets attribute values break out', () => {
  const clean = sanitizeHtml('<a href="https://x.example/&quot; onclick=&quot;alert(1)">x</a><img src="https://eu-api.asm.skype.com/v1/objects/a/views/imgo" alt="&quot; onerror=&quot;alert(1)">', {
    resolveMedia: () => ({ status: 'ok', width: 10, height: 20 })
  });
  assert.doesNotMatch(clean, /" onclick|" onerror/);
  assert.match(clean, /<img class="tm-img" src="\/api\/teams\/media\/[0-9a-f]{32}" alt="&quot; onerror=&quot;alert\(1\)" loading="lazy" width="10" height="20">/);
});

test('emoji images become their text, Teams images come from the cache and others are links', () => {
  const html = `<p>Hi <img itemtype="http://schema.skype.com/Emoji" alt="😀" src="https://statics.teams.cdn.office.net/x.png"> <img src="${IMG}" itemtype="http://schema.skype.com/AMSImage"> <img src="https://example.org/a.png" alt="pic"></p>`;
  const key = mediaKey(IMG);
  const cached = sanitizeHtml(html, { media: { [key]: { status: 'ok', width: 300, height: 200 } } });
  assert.match(cached, /Hi 😀/);
  assert.match(cached, new RegExp(`src="/api/teams/media/${key}"`));
  assert.match(cached, /<a href="https:\/\/example.org\/a.png"[^>]*>pic<\/a>/);
  const missing = sanitizeHtml(html);
  assert.match(missing, /tm-missing/);
  assert.doesNotMatch(missing, /eu-api\.asm\.skype\.com/, 'the Microsoft URL never reaches the browser');
  assert.deepEqual(extractMedia(html), [{ url: IMG, key }]);
});

test('mentions and reply quotes are kept as styled elements', () => {
  const html = '<blockquote itemtype="http://schema.skype.com/Reply" itemscope><p>old</p></blockquote><readonly><span itemtype="http://schema.skype.com/Mention" itemid="0">Anna</span></readonly> hi';
  const clean = sanitizeHtml(html);
  assert.match(clean, /<blockquote class="tm-quote"><p>old<\/p><\/blockquote>/);
  assert.match(clean, /<span class="tm-mention">Anna<\/span> hi/);
});

test('plain text messages are escaped and linkified', () => {
  const html = textToHtml('a <b> & "q" https://example.org/path?x=1&y=2.');
  assert.match(html, /a &lt;b&gt; &amp; &quot;q&quot; <a href="https:\/\/example.org\/path\?x=1&amp;y=2"[^>]*>https:\/\/example.org\/path\?x=1&amp;y=2<\/a>\.$/);
});

function raw(overrides = {}) {
  return {
    id: '1767323045006', messagetype: 'RichText/Html', content: '<p>Hello <b>there</b></p>', from: `https://emea.ng.msg.teams.microsoft.com/v1/users/ME/contacts/${ME}`,
    imdisplayname: 'Alex', originalarrivaltime: '2026-01-02T03:04:05.0060000Z', composetime: '2026-01-02T03:04:05.0060000Z', version: '1767323045006',
    properties: { files: '[]', mentions: '[]', links: '[]' }, ...overrides
  };
}

test('normalize keeps edit, delete, reaction, file and image state', () => {
  const base = normalizeMessage(raw(), { chatId: '19:a@thread.v2', myMri: ME });
  assert.equal(base.ts, Date.parse('2026-01-02T03:04:05.006Z'));
  assert.equal(base.mine, true);
  assert.equal(base.search, 'hello there ');
  assert.equal(base.deleted, false);

  const edited = normalizeMessage(raw({ properties: { edittime: '1767323999999' } }), { chatId: 'c', myMri: ME });
  assert.equal(edited.edited_at, 1767323999999);

  const deleted = normalizeMessage(raw({ content: '', properties: { deletetime: '1767324000000' } }), { chatId: 'c', myMri: ME });
  assert.equal(deleted.deleted, true);
  assert.equal(deleted.content, '');
  assert.equal(deleted.search.trim(), '');

  const rich = normalizeMessage(raw({
    content: `<p><img src="${IMG}" itemtype="http://schema.skype.com/AMSImage"></p>`,
    properties: {
      emotions: JSON.stringify([{ key: 'like', users: [{ mri: ME, time: 1 }, { mri: '8:orgid:2', time: 2 }] }]),
      files: JSON.stringify([{ id: 'f1', fileName: 'plan.xlsx', fileType: 'xlsx', objectUrl: 'https://sp.example/plan.xlsx' }])
    }
  }), { chatId: 'c', myMri: ME });
  assert.deepEqual(rich.reactions, [{ key: 'like', count: 2, mris: [ME, '8:orgid:2'], mine: true }]);
  assert.deepEqual(rich.files, [{ id: 'f1', name: 'plan.xlsx', type: 'xlsx', url: 'https://sp.example/plan.xlsx' }]);
  assert.deepEqual(rich.media, [mediaKey(IMG)]);
  assert.match(rich.search, /plan\.xlsx/);

  assert.equal(normalizeMessage(raw({ messagetype: 'Control/Typing' }), { chatId: 'c', myMri: ME }), null);
});

test('system messages and deleted messages render as labels, never as markup', () => {
  const call = normalizeMessage(raw({ messagetype: 'Event/Call', content: '<ended/><partlist alt=""><part identity="x"><name>Anna</name></part><part><name>Bo</name></part></partlist>' }), { chatId: 'c', myMri: ME });
  assert.equal(renderMessage(call).label, 'Call ended \u00b7 Anna, Bo');
  const real = normalizeMessage(raw({ messagetype: 'Event/Call', content: '<ended/><partlist alt="" count="2"><part identity="8:orgid:a"><name>8:orgid:a</name><displayName>Sune B\u00fclow</displayName><duration>1900</duration></part><part identity="8:orgid:b"><name>8:orgid:b</name><displayName>Emiel Luck</displayName><duration>1900</duration></part></partlist>' }), { chatId: 'c', myMri: ME });
  assert.equal(renderMessage(real).label, 'Call ended \u00b7 32 min \u00b7 Sune B\u00fclow, Emiel Luck');
  const names = { '8:orgid:a': 'Anna', '8:orgid:b': 'Bo', '8:orgid:c': 'Carl' };
  const nameOf = mri => names[mri] || '';
  const activity = (kind, content) => renderMessage(normalizeMessage(raw({ messagetype: kind, content }), { chatId: 'c', myMri: ME }), { nameOf }).label;
  assert.equal(activity('ThreadActivity/AddMember', '<addmember><initiator>8:orgid:a</initiator><target>8:orgid:a</target></addmember>'), 'Anna joined the chat');
  assert.equal(activity('ThreadActivity/AddMember', '<addmember><initiator>8:orgid:a</initiator><target>8:orgid:b</target><target>8:orgid:c</target><target>8:orgid:zz</target></addmember>'), 'Anna added Bo, Carl and Someone to the chat');
  assert.equal(activity('ThreadActivity/DeleteMember', '<deletemember><initiator>8:orgid:b</initiator><target>8:orgid:b</target></deletemember>'), 'Bo left the chat');
  assert.equal(activity('ThreadActivity/DeleteMember', '<deletemember><initiator>8:orgid:a</initiator><target>8:orgid:b</target></deletemember>'), 'Anna removed Bo from the chat');
  assert.equal(activity('ThreadActivity/TopicUpdate', '<topicupdate><initiator>8:orgid:a</initiator><value>Julebanko &amp; fest</value></topicupdate>'), 'Anna renamed the chat to \u201cJulebanko & fest\u201d');
  assert.equal(activity('ThreadActivity/MemberJoined', '{"initiator":"8:orgid:a","members":[{"id":"8:orgid:c","friendlyname":""},{"id":"x","friendlyname":"Peter"}]}'), 'Carl and Peter joined the chat');
  assert.deepEqual(referencedMris({ kind: 'ThreadActivity/AddMember', content: '<addmember><initiator>8:orgid:a</initiator><target>8:orgid:b</target></addmember>' }), ['8:orgid:a', '8:orgid:b']);
  assert.deepEqual(referencedMris({ kind: 'Text', content: '8:orgid:a' }), []);
  assert.equal(renderMessage(normalizeMessage(raw({ messagetype: 'ThreadActivity/AddMember', content: '<addmember></addmember>' }), { chatId: 'c', myMri: ME })).format, 'system');
  const gone = normalizeMessage(raw({ content: '', properties: { deletetime: '1' } }), { chatId: 'c', myMri: ME });
  assert.equal(renderMessage(gone).label, 'This message was deleted');
  const text = normalizeMessage(raw({ messagetype: 'Text', content: 'x &amp; <script>' }), { chatId: 'c', myMri: ME });
  assert.equal(renderMessage(text).html, 'x &amp; &lt;script&gt;');
});

test('chat kinds and member ids come from the conversation id', () => {
  assert.equal(chatKind('48:notes'), 'self');
  assert.equal(chatKind('48:notifications'), 'system');
  assert.equal(chatKind('19:meeting_abc@thread.v2'), 'meeting');
  assert.equal(chatKind('19:x@thread.tacv2'), 'channel');
  assert.equal(chatKind('19:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb@unq.gbl.spaces'), 'oneOnOne');
  assert.deepEqual(oneOnOneMris('19:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb@unq.gbl.spaces'),
    ['8:orgid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '8:orgid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb']);
  assert.equal(stripHtml('<p>a&nbsp;b</p><p>c</p>'), 'a b c');
});

test('a search snippet is cut around the match in the message\'s own letters', () => {
  const long = `${'word '.repeat(80)}The Budget Plan is here ${'tail '.repeat(80)}`;
  const snippet = searchSnippet({ kind: 'RichText/Html', content: `<p>${long}</p>` }, 'budget plan');
  assert.match(snippet, /The Budget Plan is here/);
  assert.ok(snippet.length <= 300);
  assert.equal(searchSnippet({ kind: 'Text', content: 'a &amp; b' }, 'zzz'), 'a & b');
});
