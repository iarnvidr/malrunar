// Pure helpers: Teams chatsvc messages and conversations -> the shapes kept in the
// local copy, plus the safe rendering the web view gets.
import crypto from 'node:crypto';

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', hellip: '…', ndash: '–', mdash: '—', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”' };

export function decodeEntities(text) {
  return String(text).replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (whole, body) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      try { return Number.isFinite(code) && code > 0 && code < 0x110000 ? String.fromCodePoint(code) : whole; } catch { return whole; }
    }
    return ENTITIES[body] ?? whole;
  });
}

export const escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export function stripHtml(html) {
  return decodeEntities(String(html || '')
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<img[^>]*\balt="([^"]*)"[^>]*>/gi, ' $1 ')
    .replace(/<(br|\/p|\/div|\/li|\/tr|\/blockquote)\s*\/?>/gi, ' ')
    .replace(/<[^>]*>/g, ''))
    .replace(/\s+/g, ' ').trim();
}

export function parseTs(value) {
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && /^\d+$/.test(value)) return Number(value);
  const parsed = Date.parse(value || '');
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function chatKind(id) {
  if (id.startsWith('48:')) return id === '48:notes' ? 'self' : 'system';
  if (id.includes('meeting_')) return 'meeting';
  if (id.includes('@thread.tacv2')) return 'channel';
  if (id.includes('@unq.gbl.spaces')) return 'oneOnOne';
  if (id.includes('@thread.v2')) return 'group';
  return 'chat';
}

// 19:<guid>_<guid>@unq.gbl.spaces -> the two member MRIs.
export function oneOnOneMris(id) {
  return [...id.matchAll(/(?:^19:|_)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/gi)].map(match => `8:orgid:${match[1].toLowerCase()}`);
}

export function mriFromContactLink(value) {
  const text = String(value || '');
  const match = text.match(/(8:[a-z]+:[0-9a-zA-Z._-]+)\s*$/);
  return match ? match[1] : text;
}

const AMS_HOST = /(^|\.)(asm\.skype\.com|asyncgw\.teams\.microsoft\.com)$/i;
export function isTeamsMediaUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && AMS_HOST.test(url.hostname);
  } catch { return false; }
}
export const mediaKey = url => crypto.createHash('sha256').update(String(url)).digest('hex').slice(0, 32);

function attributes(tag) {
  const found = {};
  for (const match of tag.matchAll(/([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
    found[match[1].toLowerCase()] ??= decodeEntities(match[2] ?? match[3] ?? match[4] ?? '');
  }
  return found;
}

// Cached-image candidates in a message body: AMS images and inline images.
export function extractMedia(html) {
  const result = [];
  for (const match of String(html || '').matchAll(/<img\b[^>]*>/gi)) {
    const attrs = attributes(match[0].slice(4));
    if (attrs.src && isTeamsMediaUrl(attrs.src) && !/emoji/i.test(attrs.itemtype || '')) result.push({ url: attrs.src, key: mediaKey(attrs.src) });
  }
  const seen = new Set();
  return result.filter(item => !seen.has(item.key) && seen.add(item.key));
}

function parseJson(value, fallback) {
  if (typeof value !== 'string') return value ?? fallback;
  try { return JSON.parse(value); } catch { return fallback; }
}

export function parseReactions(properties, myMri) {
  const items = parseJson(properties?.emotions, []);
  if (!Array.isArray(items)) return [];
  return items.map(item => {
    const users = Array.isArray(item?.users) ? item.users : [];
    const mris = users.map(user => String(user?.mri || '')).filter(Boolean);
    return { key: String(item?.key || ''), count: mris.length, mris: mris.slice(0, 50), mine: mris.includes(myMri) };
  }).filter(item => item.key && item.count > 0);
}

export function parseFiles(properties) {
  const items = parseJson(properties?.files, []);
  if (!Array.isArray(items)) return [];
  return items.map(item => ({
    id: String(item?.id || item?.itemid || ''),
    name: String(item?.fileName || item?.title || 'File'),
    type: String(item?.fileType || ''),
    url: String(item?.objectUrl || item?.fileInfo?.shareUrl || item?.fileInfo?.fileUrl || '')
  })).filter(item => item.name);
}

export const DISPLAYED_KINDS = new Set(['Text', 'RichText/Html', 'RichText/Media_Card', 'RichText/Media_GenericFile', 'RichText/Media_CallRecording', 'RichText/Media_CallTranscript', 'RichText/Media_FlikMsg', 'RichText/Media_AudioMsg', 'Event/Call', 'RichText', 'RichText/Giphy', 'RichText/Sticker', 'RichText/Media_VideoMsg']);
const SKIPPED = kind => !kind || kind.startsWith('Control/');
const COUNTS_AS_UNREAD = kind => kind === 'Text' || kind.startsWith('RichText');

export function isSkippedKind(kind) { return SKIPPED(kind); }
export function countsAsUnread(message) { return !message.mine && !message.deleted && COUNTS_AS_UNREAD(message.kind); }

// One raw chatsvc message -> the stored shape, or null for control messages.
export function normalizeMessage(raw, { chatId, myMri, now = Date.now() }) {
  const kind = raw.messagetype;
  if (SKIPPED(kind)) return null;
  const msgId = String(raw.id || raw.originalarrivaltime || '');
  if (!msgId) return null;
  const properties = raw.properties || {};
  const deletedAt = properties.deletetime ? parseTs(properties.deletetime) || now : 0;
  const editedAt = properties.edittime ? parseTs(properties.edittime) : 0;
  const content = typeof raw.content === 'string' ? raw.content : '';
  const fromMri = mriFromContactLink(raw.from);
  const reactions = parseReactions(properties, myMri);
  const files = parseFiles(properties);
  const media = deletedAt ? [] : extractMedia(content).map(item => item.key);
  const text = deletedAt ? '' : (kind === 'Text' ? decodeEntities(content) : stripHtml(content));
  const kept = {};
  for (const key of ['mentions', 'subject', 'pinned', 'importance', 'links']) if (properties[key] !== undefined && properties[key] !== '[]' && properties[key] !== '') kept[key] = properties[key];
  return {
    chat_id: chatId,
    msg_id: msgId,
    ts: parseTs(raw.originalarrivaltime) || parseTs(raw.composetime) || parseTs(msgId),
    version: String(raw.version || ''),
    kind,
    from_mri: fromMri,
    from_name: String(raw.imdisplayname || raw.fromDisplayNameInToken || ''),
    mine: !!myMri && fromMri === myMri,
    content: deletedAt ? '' : content,
    search: `${text} ${files.map(file => file.name).join(' ')}`.toLowerCase().slice(0, 100000),
    props: kept,
    reactions,
    files,
    media,
    edited_at: editedAt,
    deleted: !!deletedAt,
    deleted_at: deletedAt,
    root_id: raw.rootMessageId && raw.rootMessageId !== msgId ? String(raw.rootMessageId) : '',
    subject: typeof properties.subject === 'string' ? properties.subject : ''
  };
}

// The part of a stored message that changes when someone edits, deletes or reacts.
export function changeSignature(message) {
  return JSON.stringify([message.version, message.edited_at, message.deleted, message.reactions?.map(r => [r.key, r.count, r.mine])]);
}

const SYSTEM_LABELS = {
  'ThreadActivity/PictureUpdate': 'The chat picture changed',
  'ThreadActivity/HistoryDisclosedUpdate': 'Chat history sharing changed',
  'ThreadActivity/RoleUpdate': 'A role changed',
  'RichText/Media_CallRecording': 'Meeting recording',
  'RichText/Media_CallTranscript': 'Meeting transcript'
};

const MRI = /8:[a-z]+:[0-9a-zA-Z._-]+/g;
const tagValues = (text, tag) => [...String(text).matchAll(new RegExp(`<${tag}>([^<]*)</${tag}>`, 'g'))].map(match => decodeEntities(match[1]).trim()).filter(Boolean);
const parseLoose = text => { try { return JSON.parse(text); } catch { return null; } };

// The people a system message talks about, so their names can be looked up before it is shown.
export function referencedMris(message) {
  if (!message.kind?.startsWith('ThreadActivity/')) return [];
  const content = String(message.content || '');
  const json = content.startsWith('{') ? parseLoose(content) : null;
  const found = json ? [json.initiator, ...(json.members || []).map(member => member.id)] : [...content.matchAll(MRI)].map(match => match[0]);
  return [...new Set(found.filter(Boolean))];
}

const list = names => (names.length <= 3 ? names.join(', ') : `${names.slice(0, 3).join(', ')} and ${names.length - 3} more`).replace(/, ([^,]*)$/, (whole, last) => (names.length <= 3 ? ` and ${last}` : whole));

// A one-line description of a call, meeting-recording or chat-activity message.
// nameOf(mri) gives a display name or '' when it is not known.
export function systemLabel(message, nameOf = () => '') {
  const content = String(message.content || '');
  const who = mri => nameOf(mri) || 'Someone';
  if (message.kind === 'Event/Call') {
    const shown = tagValues(content, 'displayName');
    const names = shown.length ? shown : tagValues(content, 'name').filter(name => !/^8:/.test(name));
    const seconds = Number(tagValues(content, 'duration')[0]) || 0;
    const state = /<ended\b/.test(content) ? 'Call ended' : /<started\b/.test(content) ? 'Call started' : 'Call';
    const parts = [state];
    if (seconds >= 60) parts.push(`${Math.round(seconds / 60)} min`);
    if (names.length) parts.push(`${names.slice(0, 4).join(', ')}${names.length > 4 ? `, +${names.length - 4}` : ''}`);
    return parts.join(' · ');
  }
  if (message.kind === 'RichText/Media_CallRecording') {
    const title = tagValues(content, 'Title')[0];
    return title ? `Meeting recording: ${title}` : 'Meeting recording';
  }
  if (message.kind === 'ThreadActivity/AddMember') {
    const initiator = tagValues(content, 'initiator')[0];
    const targets = tagValues(content, 'target');
    const names = targets.map(who);
    if (targets.length === 1 && targets[0] === initiator) return `${names[0]} joined the chat`;
    return `${who(initiator)} added ${list(names) || 'someone'} to the chat`;
  }
  if (message.kind === 'ThreadActivity/DeleteMember') {
    const initiator = tagValues(content, 'initiator')[0];
    const target = tagValues(content, 'target')[0];
    return target && target === initiator ? `${who(target)} left the chat` : `${who(initiator)} removed ${who(target)} from the chat`;
  }
  if (message.kind === 'ThreadActivity/TopicUpdate') {
    const value = tagValues(content, 'value')[0];
    return `${who(tagValues(content, 'initiator')[0])} renamed the chat${value ? ` to “${value}”` : ''}`;
  }
  if (message.kind === 'ThreadActivity/MemberJoined' || message.kind === 'ThreadActivity/MemberLeft') {
    const json = parseLoose(content) || {};
    const names = (json.members || []).map(member => member.friendlyname || who(member.id));
    const verb = message.kind.endsWith('Joined') ? 'joined' : 'left';
    return `${list(names) || 'Someone'} ${verb} the chat`;
  }
  if (SYSTEM_LABELS[message.kind]) return SYSTEM_LABELS[message.kind];
  if (message.kind.startsWith('ThreadActivity/')) return 'Chat activity';
  return '';
}

// Plain text around the first match of a search, in the message's own letters.
export function searchSnippet(message, needle, radius = 150) {
  const text = (message.kind === 'Text' ? decodeEntities(message.content || '') : stripHtml(message.content || '')).replace(/\s+/g, ' ').trim();
  const index = text.toLowerCase().indexOf(String(needle).toLowerCase());
  if (index < 0) return text.slice(0, radius * 2);
  const start = Math.max(0, index - 60);
  return text.slice(start, start + radius * 2);
}

// ----- Safe HTML ------------------------------------------------------------

const KEEP = new Set(['p', 'br', 'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'hr', 'sub', 'sup', 'div']);
const VOID = new Set(['br', 'hr']);
const DROP_WITH_CONTENT = new Set(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'head', 'title', 'form', 'textarea', 'select', 'button', 'audio', 'video', 'canvas', 'attachment']);
const MAX_DEPTH = 60;

function safeHref(value) {
  const trimmed = String(value || '').trim();
  return /^(https?:\/\/|mailto:|tel:)/i.test(trimmed) ? trimmed : null;
}

// Rebuilds markup from an allow-list only: tag names, attributes and text are
// all re-emitted by this function, so nothing from the original survives
// unescaped.
export function sanitizeHtml(html, { media = {}, resolveMedia } = {}) {
  const source = String(html || '');
  const out = [];
  const stack = [];
  let dropDepth = 0;
  let dropTag = '';
  let position = 0;
  const pattern = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\/?([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^'">])*)>/g;
  const emitText = text => { if (!dropDepth && text) out.push(escapeHtml(decodeEntities(text))); };
  let match;
  while ((match = pattern.exec(source))) {
    emitText(source.slice(position, match.index));
    position = pattern.lastIndex;
    if (!match[1]) continue;
    const closing = match[0][1] === '/';
    const name = match[1].toLowerCase();
    if (dropDepth) {
      if (name === dropTag) { if (closing) dropDepth--; else if (!match[0].endsWith('/>')) dropDepth++; }
      continue;
    }
    if (DROP_WITH_CONTENT.has(name)) {
      if (!closing && !match[0].endsWith('/>')) { dropDepth = 1; dropTag = name; }
      continue;
    }
    const attrs = closing ? {} : attributes(match[2]);
    if (closing) {
      const index = stack.lastIndexOf(name);
      if (index >= 0) {
        while (stack.length > index) out.push(`</${stack.pop()}>`);
      }
      continue;
    }
    if (name === 'img') {
      const itemtype = attrs.itemtype || '';
      if (/emoji/i.test(itemtype)) { out.push(escapeHtml(attrs.alt || '')); continue; }
      const src = attrs.src || '';
      const key = isTeamsMediaUrl(src) ? mediaKey(src) : null;
      const info = key ? (resolveMedia ? resolveMedia(key) : media[key]) : null;
      if (info && info.status === 'ok') {
        const width = info.width || 0, height = info.height || 0;
        const ratio = width && height ? ` width="${width}" height="${height}"` : '';
        out.push(`<img class="tm-img" src="/api/teams/media/${key}" alt="${escapeHtml(attrs.alt || 'image')}" loading="lazy"${ratio}>`);
      } else if (key) {
        out.push(`<span class="tm-missing">[image not available]</span>`);
      } else if (safeHref(src)) {
        out.push(`<a href="${escapeHtml(src)}" target="_blank" rel="noopener noreferrer nofollow">${escapeHtml(attrs.alt || 'external image')}</a>`);
      }
      continue;
    }
    if (name === 'a') {
      const href = safeHref(attrs.href);
      if (href && stack.length < MAX_DEPTH) { out.push(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer nofollow">`); stack.push('a'); }
      continue;
    }
    if (name === 'span' && /mention/i.test(attrs.itemtype || '')) {
      if (stack.length < MAX_DEPTH) { out.push('<span class="tm-mention">'); stack.push('span'); }
      continue;
    }
    if (name === 'blockquote' && /reply/i.test(attrs.itemtype || '')) {
      if (stack.length < MAX_DEPTH) { out.push('<blockquote class="tm-quote">'); stack.push('blockquote'); }
      continue;
    }
    if (KEEP.has(name)) {
      if (VOID.has(name)) { out.push(`<${name}>`); continue; }
      if (stack.length < MAX_DEPTH) { out.push(`<${name}>`); stack.push(name); }
    }
    // Any other tag (span, font, readonly, ...) is dropped; its text stays.
  }
  emitText(source.slice(position));
  while (stack.length) out.push(`</${stack.pop()}>`);
  return out.join('');
}

const URL_PATTERN = /\bhttps?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]]/g;
export function textToHtml(text) {
  const escaped = escapeHtml(text);
  return escaped.replace(URL_PATTERN, url => `<a href="${url}" target="_blank" rel="noopener noreferrer nofollow">${url}</a>`);
}

// What the web view shows for one stored message.
export function renderMessage(message, { resolveMedia, nameOf } = {}) {
  const base = {
    id: message.msg_id, chatId: message.chat_id, ts: message.ts, kind: message.kind, from: message.from_name, fromMri: message.from_mri,
    mine: !!message.mine, edited: message.edited_at || 0, deleted: !!message.deleted, reactions: message.reactions || [],
    files: message.files || [], subject: message.subject || '', rootId: message.root_id || ''
  };
  if (message.deleted) return { ...base, format: 'system', html: '', label: 'This message was deleted' };
  const label = systemLabel(message, nameOf);
  if (label) return { ...base, format: 'system', html: '', label };
  if (message.kind === 'Text') return { ...base, format: 'text', html: textToHtml(decodeEntities(message.content)) };
  const html = sanitizeHtml(message.content, { resolveMedia });
  if (!html.trim() && !(message.files || []).length) {
    return { ...base, format: 'system', html: '', label: message.kind === 'RichText/Media_Card' ? 'Card message (open it in Teams)' : 'Message without text' };
  }
  return { ...base, format: 'html', html };
}
