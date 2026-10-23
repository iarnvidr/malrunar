// The local copy. PbStore keeps it in PocketBase; MemoryStore is the same
// interface in memory for tests (both run the same contract tests).
import { quote } from './pb.mjs';
import { changeSignature, countsAsUnread } from './messages.mjs';

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const UNREAD_FILTER = "mine = false && deleted = false && (kind = 'Text' || kind ~ 'RichText')";

function applyChatPatch(existing, patch) {
  const changed = {};
  for (const [key, value] of Object.entries(patch)) if (!same(existing?.[key], value)) changed[key] = value;
  return changed;
}

const byRecency = (a, b) => (b.last_at || 0) - (a.last_at || 0) || String(a.chat_id).localeCompare(String(b.chat_id));
function chatMatches(chat, { kind, q, includeHidden, unreadOnly }) {
  if (kind && kind !== 'all') {
    if (kind === 'chats' ? !['oneOnOne', 'group', 'self', 'chat'].includes(chat.kind) : chat.kind !== kind) return false;
  }
  if (!includeHidden && chat.hidden) return false;
  if (unreadOnly && !(chat.unread > 0)) return false;
  if (q && !String(chat.search || chat.title || '').includes(q.toLowerCase())) return false;
  return true;
}

export class PbStore {
  constructor(pb) {
    this.pb = pb;
    this.chats = new Map();      // chat_id -> record (all chats; a few hundred)
    this.people = new Map();     // mri -> record
    this.media = new Map();      // key -> record (loaded on demand)
    this.stateRows = new Map();  // k -> record
  }

  async init() {
    for (const row of await this.pb.listAll('chats')) this.chats.set(row.chat_id, row);
    for (const row of await this.pb.listAll('people')) this.people.set(row.mri, row);
    for (const row of await this.pb.listAll('state')) this.stateRows.set(row.k, row);
  }

  // ----- chats
  getChat(chatId) { return this.chats.get(chatId) || null; }

  async upsertChat(chatId, patch) {
    const existing = this.chats.get(chatId);
    if (!existing) {
      const created = await this.pb.create('chats', { chat_id: chatId, ...patch });
      this.chats.set(chatId, created);
      return created;
    }
    const changed = applyChatPatch(existing, patch);
    if (!Object.keys(changed).length) return existing;
    const updated = await this.pb.update('chats', existing.id, changed);
    this.chats.set(chatId, updated);
    return updated;
  }

  listChats({ kind, q, includeHidden = false, unreadOnly = false, limit = 200, offset = 0 } = {}) {
    return [...this.chats.values()].filter(chat => chatMatches(chat, { kind, q, includeHidden, unreadOnly })).sort(byRecency).slice(offset, offset + limit);
  }

  allChats() { return [...this.chats.values()].sort(byRecency); }

  // ----- messages
  async messagesInRange(chatId, from, to) {
    return this.pb.listAll('messages', { filter: `chat_id = ${quote(chatId)} && ts >= ${from} && ts <= ${to}`, sort: 'ts' });
  }

  // Inserts new messages and updates those whose edit/delete/reaction state changed.
  async upsertMessages(chatId, messages) {
    if (!messages.length) return { created: [], updated: [] };
    const low = Math.min(...messages.map(message => message.ts)), high = Math.max(...messages.map(message => message.ts));
    const existing = new Map((await this.messagesInRange(chatId, low - 1, high + 1)).map(row => [row.msg_id, row]));
    const created = [], updated = [];
    const now = Date.now();
    for (const message of messages) {
      const row = existing.get(message.msg_id);
      if (!row) { await this.pb.create('messages', { ...message, synced_at: now }); created.push(message.msg_id); continue; }
      if (changeSignature(row) !== changeSignature(message) || row.content !== message.content) {
        await this.pb.update('messages', row.id, { ...message, synced_at: now });
        updated.push(message.msg_id);
      }
    }
    return { created, updated };
  }

  async listMessages(chatId, { beforeTs, limit = 50 } = {}) {
    const filter = `chat_id = ${quote(chatId)}${beforeTs ? ` && ts < ${beforeTs}` : ''}`;
    const rows = await this.pb.list('messages', { filter, sort: '-ts', perPage: limit + 1 });
    return { messages: rows.slice(0, limit).reverse(), hasMore: rows.length > limit };
  }

  async messagesAround(chatId, ts, before = 25, after = 25) {
    const earlier = await this.pb.list('messages', { filter: `chat_id = ${quote(chatId)} && ts <= ${ts}`, sort: '-ts', perPage: before + 1 });
    const later = await this.pb.list('messages', { filter: `chat_id = ${quote(chatId)} && ts > ${ts}`, sort: 'ts', perPage: after });
    return { messages: [...earlier.slice(0, before + 1).reverse(), ...later], hasMore: earlier.length > before };
  }

  async getMessage(chatId, msgId) {
    return this.pb.first('messages', `chat_id = ${quote(chatId)} && msg_id = ${quote(msgId)}`);
  }

  async latestMessages(chatId, limit = 30) {
    return (await this.pb.list('messages', { filter: `chat_id = ${quote(chatId)}`, sort: '-ts', perPage: limit })).reverse();
  }

  async newestMine(chatId, afterTs) {
    return this.pb.first('messages', `chat_id = ${quote(chatId)} && mine = true && ts >= ${afterTs}`, { sort: '-ts' });
  }

  async countMessages(chatId) { return this.pb.count('messages', `chat_id = ${quote(chatId)}`); }

  async countUnread(chatId, horizonTs) {
    return this.pb.count('messages', `chat_id = ${quote(chatId)} && ts > ${Number(horizonTs) || 0} && ${UNREAD_FILTER}`);
  }

  async search(q, { limit = 40, chatId } = {}) {
    const needle = q.toLowerCase().trim();
    if (!needle) return [];
    return this.pb.list('messages', { filter: `search ~ ${quote(needle)}${chatId ? ` && chat_id = ${quote(chatId)}` : ''} && deleted = false`, sort: '-ts', perPage: limit });
  }

  async newestTs(chatId) { return (await this.pb.first('messages', `chat_id = ${quote(chatId)}`, { sort: '-ts', fields: 'ts' }))?.ts || 0; }

  // ----- people
  getPerson(mri) { return this.people.get(mri) || null; }
  async savePeople(entries) {
    const now = Date.now();
    for (const entry of entries) {
      const existing = this.people.get(entry.mri);
      const data = { mri: entry.mri, name: entry.name || '', email: entry.email || '', updated_at: now };
      if (existing && existing.name === data.name && existing.email === data.email) continue;
      this.people.set(entry.mri, existing ? await this.pb.update('people', existing.id, data) : await this.pb.create('people', data));
    }
  }

  // ----- media
  async getMedia(key) {
    if (this.media.has(key)) return this.media.get(key);
    const row = await this.pb.first('media', `key = ${quote(key)}`);
    if (row) this.media.set(key, row);
    return row;
  }
  async mediaForKeys(keys) {
    const result = {};
    const missing = keys.filter(key => !this.media.has(key));
    for (let index = 0; index < missing.length; index += 40) {
      const batch = missing.slice(index, index + 40);
      for (const row of await this.pb.list('media', { filter: batch.map(key => `key = ${quote(key)}`).join(' || '), perPage: 100 })) this.media.set(row.key, row);
    }
    for (const key of keys) if (this.media.has(key)) result[key] = this.media.get(key);
    return result;
  }
  async saveMedia(record) {
    const existing = await this.getMedia(record.key);
    const saved = existing ? await this.pb.update('media', existing.id, record) : await this.pb.create('media', record);
    this.media.set(record.key, saved);
    return saved;
  }
  async pendingMedia(limit = 20, retryBefore = Date.now() - 10 * 60 * 1000) {
    return this.pb.list('media', { filter: `(status = 'pending') || (status = 'failed' && attempts < 4 && fetched_at < ${retryBefore})`, sort: 'fetched_at', perPage: limit });
  }
  async mediaCounts() {
    return { ok: await this.pb.count('media', "status = 'ok'"), pending: await this.pb.count('media', "status = 'pending'"), failed: await this.pb.count('media', "status = 'failed'") };
  }

  // ----- state
  getState(key, fallback = null) { return this.stateRows.has(key) ? this.stateRows.get(key).v : fallback; }
  async setState(key, value) {
    const existing = this.stateRows.get(key);
    if (existing && same(existing.v, value)) return;
    this.stateRows.set(key, existing ? await this.pb.update('state', existing.id, { v: value }) : await this.pb.create('state', { k: key, v: value }));
  }

  async stats() {
    return { chats: this.chats.size, messages: await this.pb.count('messages', ''), people: this.people.size };
  }
}

// ---------------------------------------------------------------------------

export class MemoryStore {
  constructor() {
    this.chats = new Map(); this.rows = []; this.people = new Map(); this.mediaRows = new Map(); this.stateRows = new Map(); this.nextId = 1;
  }
  async init() {}

  getChat(chatId) { return this.chats.get(chatId) || null; }
  async upsertChat(chatId, patch) {
    const existing = this.chats.get(chatId);
    const next = existing ? { ...existing, ...patch } : { id: `c${this.nextId++}`, chat_id: chatId, ...patch };
    this.chats.set(chatId, next);
    return next;
  }
  listChats({ kind, q, includeHidden = false, unreadOnly = false, limit = 200, offset = 0 } = {}) {
    return [...this.chats.values()].filter(chat => chatMatches(chat, { kind, q, includeHidden, unreadOnly })).sort(byRecency).slice(offset, offset + limit);
  }
  allChats() { return [...this.chats.values()].sort(byRecency); }

  async messagesInRange(chatId, from, to) {
    return this.rows.filter(row => row.chat_id === chatId && row.ts >= from && row.ts <= to).sort((a, b) => a.ts - b.ts);
  }
  async upsertMessages(chatId, messages) {
    const created = [], updated = [];
    for (const message of messages) {
      const index = this.rows.findIndex(row => row.chat_id === chatId && row.msg_id === message.msg_id);
      if (index < 0) { this.rows.push({ id: `m${this.nextId++}`, ...message, synced_at: Date.now() }); created.push(message.msg_id); continue; }
      const row = this.rows[index];
      if (changeSignature(row) !== changeSignature(message) || row.content !== message.content) {
        this.rows[index] = { ...row, ...message, synced_at: Date.now() };
        updated.push(message.msg_id);
      }
    }
    return { created, updated };
  }
  async listMessages(chatId, { beforeTs, limit = 50 } = {}) {
    const rows = this.rows.filter(row => row.chat_id === chatId && (!beforeTs || row.ts < beforeTs)).sort((a, b) => b.ts - a.ts);
    return { messages: rows.slice(0, limit).reverse(), hasMore: rows.length > limit };
  }
  async messagesAround(chatId, ts, before = 25, after = 25) {
    const rows = this.rows.filter(row => row.chat_id === chatId).sort((a, b) => a.ts - b.ts);
    const earlier = rows.filter(row => row.ts <= ts), later = rows.filter(row => row.ts > ts);
    return { messages: [...earlier.slice(-(before + 1)), ...later.slice(0, after)], hasMore: earlier.length > before + 1 };
  }
  async getMessage(chatId, msgId) { return this.rows.find(row => row.chat_id === chatId && row.msg_id === msgId) || null; }
  async latestMessages(chatId, limit = 30) { return this.rows.filter(row => row.chat_id === chatId).sort((a, b) => a.ts - b.ts).slice(-limit); }
  async newestMine(chatId, afterTs) { return this.rows.filter(row => row.chat_id === chatId && row.mine && row.ts >= afterTs).sort((a, b) => b.ts - a.ts)[0] || null; }
  async countMessages(chatId) { return this.rows.filter(row => row.chat_id === chatId).length; }
  async countUnread(chatId, horizonTs) { return this.rows.filter(row => row.chat_id === chatId && row.ts > (Number(horizonTs) || 0) && countsAsUnread(row)).length; }
  async search(q, { limit = 40, chatId } = {}) {
    const needle = q.toLowerCase().trim();
    if (!needle) return [];
    return this.rows.filter(row => row.search.includes(needle) && !row.deleted && (!chatId || row.chat_id === chatId)).sort((a, b) => b.ts - a.ts).slice(0, limit);
  }
  async newestTs(chatId) { return Math.max(0, ...this.rows.filter(row => row.chat_id === chatId).map(row => row.ts)); }

  getPerson(mri) { return this.people.get(mri) || null; }
  async savePeople(entries) { for (const entry of entries) this.people.set(entry.mri, { mri: entry.mri, name: entry.name || '', email: entry.email || '', updated_at: Date.now() }); }

  async getMedia(key) { return this.mediaRows.get(key) || null; }
  async mediaForKeys(keys) { return Object.fromEntries(keys.filter(key => this.mediaRows.has(key)).map(key => [key, this.mediaRows.get(key)])); }
  async saveMedia(record) { const saved = { ...(this.mediaRows.get(record.key) || {}), ...record }; this.mediaRows.set(record.key, saved); return saved; }
  async pendingMedia(limit = 20, retryBefore = Date.now() - 10 * 60 * 1000) {
    return [...this.mediaRows.values()].filter(row => row.status === 'pending' || (row.status === 'failed' && row.attempts < 4 && row.fetched_at < retryBefore)).slice(0, limit);
  }
  async mediaCounts() {
    const rows = [...this.mediaRows.values()];
    return { ok: rows.filter(row => row.status === 'ok').length, pending: rows.filter(row => row.status === 'pending').length, failed: rows.filter(row => row.status === 'failed').length };
  }

  getState(key, fallback = null) { return this.stateRows.has(key) ? this.stateRows.get(key) : fallback; }
  async setState(key, value) { this.stateRows.set(key, value); }
  async stats() { return { chats: this.chats.size, messages: this.rows.length, people: this.people.size }; }
}
