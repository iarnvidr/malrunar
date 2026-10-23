// The Teams service's HTTP API. Loopback only, and every route but /health
// needs the bearer token that only the console and this service are started
// with. Reads come from the local copy; writes go live to Teams and fail
// visibly when the token is gone.
import http from 'node:http';
import fs from 'node:fs';
import crypto from 'node:crypto';
import { escapeHtml, mediaKey, renderMessage, searchSnippet } from './messages.mjs';
import { imageSize } from './media.mjs';
import { PRIORITY, TeamsError } from './teams.mjs';
import { GraphError } from './calendar-auth.mjs';

const MAX_BODY = 1024 * 1024;
const MAX_TEXT = 28000;
const REACTION_KEY = /^[a-z0-9_]{1,60}$/;
const MAX_UPLOAD = 10 * 1024 * 1024;
const MAX_ATTACHMENTS = 5;
const UPLOAD_TTL_MS = 60 * 60 * 1000;
const IMAGE_TYPES = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' };

class HttpError extends Error {
  constructor(status, message, extra = {}) { super(message); this.status = status; this.extra = extra; }
}

const json = (response, status, body, headers = {}) => {
  const payload = JSON.stringify(body);
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Content-Length': Buffer.byteLength(payload), ...headers });
  response.end(payload);
};

async function readBytes(request, limit = MAX_BODY) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new HttpError(413, 'The request is too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function readJson(request) {
  const bytes = await readBytes(request);
  try {
    const body = JSON.parse(bytes.toString('utf8') || '{}');
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('not an object');
    return body;
  } catch { throw new HttpError(400, 'Send a JSON object'); }
}

const intParam = (value, fallback, low, high) => { const number = Number.parseInt(value, 10); return Number.isFinite(number) ? Math.max(low, Math.min(high, number)) : fallback; };

export function createServer({ token, store, sync, teams, auth, media, files, calendar, myName = () => '', stats = () => ({}), now = () => Date.now() }) {
  const expected = Buffer.from(token);
  const authorised = request => {
    const header = String(request.headers.authorization || '');
    const given = Buffer.from(header.startsWith('Bearer ') ? header.slice(7) : '');
    return given.length === expected.length && crypto.timingSafeEqual(given, expected);
  };

  const personName = mri => store.getPerson(mri)?.name || '';

  async function renderList(rows) {
    const keys = [...new Set(rows.flatMap(row => [...(row.media || []), ...(row.files || []).filter(file => file.url).map(file => mediaKey(file.url))]))];
    const mediaMap = keys.length ? await store.mediaForKeys(keys) : {};
    const resolveMedia = key => mediaMap[key];
    return rows.map(row => {
      const view = renderMessage(row, { resolveMedia, nameOf: personName });
      view.files = view.files.map(file => {
        const key = mediaKey(file.url);
        const cached = mediaMap[key]?.status === 'ok';
        return { ...file, cached, url: cached ? `/api/teams/media/${key}?name=${encodeURIComponent(file.name)}` : file.url };
      });
      if (view.mine) view.from = 'You';
      view.reactions = view.reactions.map(reaction => ({ key: reaction.key, count: reaction.count, mine: reaction.mine, names: reaction.mris.slice(0, 12).map(mri => personName(mri) || '…') }));
      return view;
    });
  }

  function chatView(chat) {
    const mine = chat.preview_from && myName() && chat.preview_from === myName();
    return {
      id: chat.chat_id, kind: chat.kind, title: chat.title || chat.topic || 'Chat', preview: chat.preview || '',
      previewFrom: mine ? 'You' : chat.preview_from || '', lastAt: chat.last_at || 0, unread: chat.unread || 0,
      hidden: !!chat.hidden, isSelf: !!chat.is_self, memberCount: chat.member_count || (chat.members || []).length,
      historyComplete: !!chat.backfill_done
    };
  }

  function requireChat(chatId) {
    const chat = store.getChat(chatId);
    if (!chat) throw new HttpError(404, 'No such chat');
    return chat;
  }

  const requireMessageId = value => { if (!/^\d{6,20}$/.test(value || '')) throw new HttpError(400, 'Choose a message'); return value; };
  const text = body => {
    const value = typeof body.text === 'string' ? body.text.replace(/\r\n/g, '\n').trim() : '';
    if (!value) throw new HttpError(400, 'Write something first');
    if (value.length > MAX_TEXT) throw new HttpError(400, 'The message is too long');
    return value;
  };

  // A write that Teams refused because the session is gone says so; nothing is retried or queued.
  async function live(run) {
    await auth.ensure();
    if (!auth.status().haveTokens || auth.status().state === 'signed-out') {
      throw new HttpError(503, 'Teams is signed out: reading works, but nothing can be sent, edited or reacted to until it is signed in again.', { signedOut: true });
    }
    try { return await run(); } catch (error) {
      if (error instanceof TeamsError) {
        if (error.authFailure) throw new HttpError(503, 'Teams is signed out: the change was not made.', { signedOut: true });
        throw new HttpError(502, `Teams did not accept the change: ${error.message}`);
      }
      throw error;
    }
  }

  // Files waiting to be attached to a message: id -> { chatId, ... }. Kept in memory only.
  const uploads = new Map();
  const purgeUploads = () => {
    for (const [id, entry] of uploads) if (now() - entry.at > UPLOAD_TTL_MS) uploads.delete(id);
    while (uploads.size > 40) uploads.delete(uploads.keys().next().value);
  };

  const routes = [];
  const route = (method, pattern, handler, options = {}) => routes.push({ method, pattern: new RegExp(`^${pattern}$`), handler, raw: !!options.raw });
  const seg = '([^/]+)';

  if (calendar) {
    route('GET', '/calendar/status', async () => calendar.status());
    route('GET', '/calendar/events', async ({ query }) => ({ events: calendar.list({ start: query.get('start'), end: query.get('end') }) }));
    route('GET', `/calendar/events/${seg}`, async ({ params }) => {
      const event = calendar.get(params[0]);
      if (!event) throw new HttpError(404, 'No such event');
      return { event };
    });
    route('POST', '/calendar/sync', async () => { calendar.sync().catch(() => {}); return { ok: true }; });
    route('POST', '/calendar/events', async ({ body }) => ({ ok: true, event: await calendar.create(body) }));
    route('POST', `/calendar/events/${seg}/update`, async ({ params, body }) => ({ ok: true, event: await calendar.update(params[0], body) }));
    route('POST', `/calendar/events/${seg}/respond`, async ({ params, body }) => ({ ok: true, event: await calendar.respond(params[0], body) }));
    route('POST', `/calendar/events/${seg}/delete`, async ({ params }) => { await calendar.remove(params[0]); return { ok: true }; });
  }

  route('GET', '/health', async () => ({ ok: true }));

  route('GET', '/status', async () => {
    const memory = process.memoryUsage();
    return {
      auth: auth.status(), sync: sync.status(), counts: await store.stats(), media: await store.mediaCounts(),
      me: myName(), rssMb: Math.round(memory.rss / 1048576), now: now(), ...stats()
    };
  });

  route('GET', '/chats', async ({ query }) => {
    const filters = { kind: query.get('kind') || 'all', q: query.get('q') || '', unreadOnly: query.get('unread') === '1' };
    const limit = intParam(query.get('limit'), 300, 1, 1000), offset = intParam(query.get('offset'), 0, 0, 100000);
    const chats = store.listChats({ ...filters, limit, offset });
    const totalMatches = store.listChats({ ...filters, limit: 100000 }).length;
    const all = store.listChats({ includeHidden: false, limit: 100000 });
    const unreadChats = all.filter(chat => chat.unread > 0 && chat.kind !== 'meeting').length;
    return { chats: chats.map(chatView), totalMatches, offset, limit, hasMore: offset + chats.length < totalMatches,
      unreadChats, unreadMeetings: all.filter(chat => chat.unread > 0 && chat.kind === 'meeting').length };
  });

  route('GET', `/chats/${seg}`, async ({ params }) => {
    const chat = requireChat(params[0]);
    return {
      ...chatView(chat), members: (chat.members || []).slice(0, 40).map(member => ({ mri: member.mri, name: personName(member.mri) || '', role: member.role || '' })),
      messageCount: chat.message_count || 0, readHorizon: chat.read_horizon || 0
    };
  });

  route('GET', `/chats/${seg}/messages`, async ({ params, query }) => {
    requireChat(params[0]);
    const limit = intParam(query.get('limit'), 50, 1, 200);
    const aroundTs = intParam(query.get('around'), 0, 0, Number.MAX_SAFE_INTEGER);
    const page = aroundTs ? await store.messagesAround(params[0], aroundTs) : await store.listMessages(params[0], { beforeTs: intParam(query.get('before'), 0, 0, Number.MAX_SAFE_INTEGER), limit });
    const chat = store.getChat(params[0]);
    return { messages: await renderList(page.messages), hasMore: page.hasMore, historyComplete: !!chat.backfill_done, readHorizon: chat.read_horizon || 0 };
  });

  route('GET', '/search', async ({ query }) => {
    const q = (query.get('q') || '').trim();
    if (q.length < 2) return { results: [], chats: [] };
    const rows = await store.search(q, { limit: intParam(query.get('limit'), 40, 1, 100), chatId: query.get('chat') || undefined });
    const views = await renderList(rows);
    return {
      results: views.map(view => ({ ...view, chatTitle: store.getChat(view.chatId)?.title || '', snippet: searchSnippet(rows.find(row => row.msg_id === view.id && row.chat_id === view.chatId) || {}, q)})),
      chats: store.listChats({ q, limit: 10, kind: 'all' }).map(chatView)
    };
  });

  route('GET', `/media/${seg}`, async ({ params, query, response }) => {
    const entry = await media.read(params[0]);
    if (!entry) throw new HttpError(404, 'No such image');
    response.writeHead(200, {
      'Content-Type': entry.contentType, 'Content-Length': entry.size, 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': 'sandbox',
      'Cache-Control': 'private, max-age=31536000, immutable', 'Referrer-Policy': 'no-referrer',
      ...(entry.attachment ? { 'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent((query.get('name') || 'file').replace(/[\r\n]/g, '').slice(0, 200))}` } : {})
    });
    fs.createReadStream(entry.file).pipe(response);
    return undefined;
  });

  route('POST', '/sync', async () => { sync.pollOnce().catch(() => {}); return { ok: true }; });

  // Uploads go to the caller's Teams Chat Files area and are shared with the
  // chat members. /send adds the normal Teams file metadata to the message.
  route('POST', `/chats/${seg}/upload`, async ({ params, rawBody, request }) => {
    const chat = requireChat(params[0]);
    const type = String(request.headers['x-file-type'] || '').split(';')[0].trim().toLowerCase();
    if (!files) throw new HttpError(415, 'File uploads are unavailable');
    if (!rawBody.length) throw new HttpError(400, 'The file is empty');
    if (rawBody.length > MAX_UPLOAD) throw new HttpError(413, 'Files can be at most 10 MB');
    const rawName = (() => { try { return decodeURIComponent(String(request.headers['x-file-name'] || '')); } catch { return ''; } })();
    const name = (rawName.replace(/[\\/\u0000-\u001f"<>:|?*]/g, '_').trim() || (IMAGE_TYPES[type] ? `image.${IMAGE_TYPES[type]}` : 'file')).slice(0, 120);
    const { width, height } = imageSize(rawBody);
    purgeUploads();
    return live(async () => {
      const memberMris = (chat.members || []).map(member => member.mri).filter(mri => mri !== teams.me());
      if (!chat.is_self && (!memberMris.length || chat.member_count > (chat.members || []).length)) throw new HttpError(409, 'The full member list is needed to share this file. Open this chat in Teams.');
      const result = await files.upload({ bytes: rawBody, name, type, memberMris });
      const id = crypto.randomBytes(12).toString('hex');
      uploads.set(id, { ...result, id: result.id, chatId: chat.chat_id, at: now(), name, type: result.type || type, width, height, size: rawBody.length, isFile: true });
      return { ok: true, uploadId: id, name, size: rawBody.length, width, height };
    });
  }, { raw: true });

  const textHtml = value => `<p>${escapeHtml(value).replace(/\n/g, '<br>')}</p>`;

  route('POST', `/chats/${seg}/send`, async ({ params, body }) => {
    const chat = requireChat(params[0]);
    const ids = Array.isArray(body.attachments) ? body.attachments.map(String) : [];
    if (ids.length > MAX_ATTACHMENTS) throw new HttpError(400, `At most ${MAX_ATTACHMENTS} files per message`);
    const entries = ids.map(id => {
      const entry = uploads.get(id);
      if (!entry || entry.chatId !== chat.chat_id) throw new HttpError(400, 'An attached file is no longer available. Attach it again.');
      return entry;
    });
    const hasText = typeof body.text === 'string' && body.text.trim().length > 0;
    if (!hasText && !entries.length) throw new HttpError(400, 'Write something first');
    const content = hasText ? text(body) : '';
    const startedAt = now();
    return live(async () => {
      if (entries.length) await teams.send(chat.chat_id, (content ? textHtml(content) : '') + entries.map(entry => `<p><a href="${escapeHtml(entry.url)}">${escapeHtml(entry.name)}</a></p>`).join(''), { html: true, files: entries });
      else await teams.send(chat.chat_id, content);
      for (const id of ids) uploads.delete(id);
      await sync.syncChatRecent(chat.chat_id, { pageSize: 20, priority: PRIORITY.interactive });
      const row = await store.newestMine(chat.chat_id, startedAt - 60000);
      return { ok: true, message: row ? (await renderList([row]))[0] : null };
    });
  });

  route('POST', `/chats/${seg}/messages/${seg}/edit`, async ({ params, body }) => {
    const chat = requireChat(params[0]);
    const messageId = requireMessageId(params[1]);
    const row = await store.getMessage(chat.chat_id, messageId);
    if (!row) throw new HttpError(404, 'No such message');
    if (!row.mine) throw new HttpError(403, 'Only your own messages can be edited');
    if (row.deleted) throw new HttpError(409, 'This message was deleted');
    if (/<img\b|itemtype="[^"]*(mention|reply)|<(table|ul|ol|pre)\b/i.test(row.content || '') || (row.files || []).length) {
      throw new HttpError(409, 'This message has images, files, mentions, quotes or formatting that would be lost here. Edit it in Teams.');
    }
    const content = text(body);
    return live(async () => {
      await teams.edit(chat.chat_id, messageId, content);
      await sync.refreshMessage(chat.chat_id, messageId);
      return { ok: true, message: (await renderList([await store.getMessage(chat.chat_id, messageId)]))[0] };
    });
  });

  route('POST', `/chats/${seg}/messages/${seg}/delete`, async ({ params }) => {
    const chat = requireChat(params[0]);
    const messageId = requireMessageId(params[1]);
    const row = await store.getMessage(chat.chat_id, messageId);
    if (!row) throw new HttpError(404, 'No such message');
    if (!row.mine) throw new HttpError(403, 'Only your own messages can be deleted');
    return live(async () => {
      await teams.remove(chat.chat_id, messageId);
      await sync.refreshMessage(chat.chat_id, messageId);
      return { ok: true, message: (await renderList([await store.getMessage(chat.chat_id, messageId)]))[0] };
    });
  });

  route('POST', `/chats/${seg}/messages/${seg}/reaction`, async ({ params, body }) => {
    const chat = requireChat(params[0]);
    const messageId = requireMessageId(params[1]);
    if (!(await store.getMessage(chat.chat_id, messageId))) throw new HttpError(404, 'No such message');
    const key = String(body.key || '');
    if (!REACTION_KEY.test(key)) throw new HttpError(400, 'Choose a reaction');
    return live(async () => {
      await (body.on === false ? teams.unreact : teams.react)(chat.chat_id, messageId, key);
      await sync.refreshMessage(chat.chat_id, messageId);
      return { ok: true, message: (await renderList([await store.getMessage(chat.chat_id, messageId)]))[0] };
    });
  });

  route('POST', `/chats/${seg}/read`, async ({ params, body }) => {
    const chat = requireChat(params[0]);
    const newest = body.messageId ? await store.getMessage(chat.chat_id, requireMessageId(String(body.messageId))) : (await store.latestMessages(chat.chat_id, 1))[0];
    if (!newest) return { ok: true, unread: 0 };
    return live(async () => {
      await teams.markRead(chat.chat_id, newest.msg_id);
      await store.upsertChat(chat.chat_id, { read_horizon: Math.max(chat.read_horizon || 0, newest.ts) });
      await sync.recomputeUnread(chat.chat_id);
      return { ok: true, unread: store.getChat(chat.chat_id).unread };
    });
  });

  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://127.0.0.1');
      const path = url.pathname;
      if (path !== '/health' && !authorised(request)) { json(response, 401, { error: 'Not authorised' }); return; }
      for (const entry of routes) {
        if (entry.method !== request.method) continue;
        const match = entry.pattern.exec(path);
        if (!match) continue;
        const params = match.slice(1).map(part => { try { return decodeURIComponent(part); } catch { throw new HttpError(400, 'Bad path'); } });
        const rawBody = entry.raw && request.method === 'POST' ? await readBytes(request, MAX_UPLOAD + 1024) : null;
        const body = request.method === 'POST' && !entry.raw ? await readJson(request) : {};
        const result = await entry.handler({ params, query: url.searchParams, body, rawBody, response, request });
        if (result !== undefined) json(response, 200, result);
        return;
      }
      json(response, 404, { error: 'No such route' });
    } catch (error) {
      if (response.headersSent) { response.destroy(); return; }
      if (error instanceof HttpError) { json(response, error.status, { error: error.message, ...error.extra }); return; }
      if (error instanceof GraphError) {
        json(response, error.signedOut ? 503 : error.status >= 400 && error.status < 500 ? error.status : 502,
          { error: error.message, ...(error.signedOut ? { signedOut: true } : {}) });
        return;
      }
      json(response, 500, { error: `Teams service error: ${String(error.message || error).slice(0, 200)}` });
    }
  });
  return server;
}
