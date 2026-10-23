// Keeps the local copy in step with Teams: chat list, messages (with edit and
// delete state), people and image bytes. Three loops share one paced request
// queue (teams.mjs): a poll every 10-15 s, a slow history backfill, and media
// downloads. The step functions are separate so tests can drive them directly.
import { PRIORITY } from './teams.mjs';
import { chatKind, decodeEntities, extractMedia, mediaKey, normalizeMessage, oneOnOneMris, parseTs, referencedMris, stripHtml, systemLabel, isSkippedKind } from './messages.mjs';

const POLL_PAGE = 40;
const RECENT_MESSAGES = 30;
const BACKFILL_PAGE = 100;
const MAX_MEMBERS_KEPT = 40;
const FULL_LIST_EVERY_MS = 10 * 60 * 1000;
const RECHECK_CHATS_PER_POLL = 2;
const RECHECK_WINDOW = 60;

export function createSync({ store, teams, media, auth, log = () => {}, now = () => Date.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
                             pollMs = 12000, backfillGapMs = 700, random = Math.random }) {
  const status = { startedAt: now(), lastPollAt: 0, lastPollOk: 0, lastPollError: '', lastFullListAt: 0, backfill: { total: 0, done: 0, current: '' }, paused: '', newMessages: 0 };
  let stopped = false;
  let myMri = '';
  let fullListAt = 0;
  let recheckCursor = 0;
  const me = () => myMri || (myMri = teams.me());

  // ----- names
  const personName = mri => store.getPerson(mri)?.name || '';

  async function learnPeople(messages) {
    const seen = new Map();
    for (const message of messages) if (message.from_mri && message.from_name && !seen.has(message.from_mri)) seen.set(message.from_mri, message.from_name);
    const fresh = [...seen].filter(([mri]) => !personName(mri)).map(([mri, name]) => ({ mri, name, email: store.getPerson(mri)?.email || '' }));
    if (fresh.length) await store.savePeople(fresh);
  }

  async function resolveMissing(mris) {
    const missing = [...new Set(mris)].filter(mri => mri && !personName(mri));
    for (let index = 0; index < missing.length; index += 40) {
      try { await store.savePeople(await teams.names(missing.slice(index, index + 40))); } catch (error) { log('warn', `Could not resolve names: ${error.message}`); }
    }
  }

  function computeTitle(chat) {
    if (chat.is_self) return 'Notes (you)';
    if (chat.topic) return chat.topic;
    const others = (chat.members || []).map(member => member.mri).filter(mri => mri !== me());
    const names = others.map(personName).filter(Boolean);
    if (chat.kind === 'oneOnOne') return names[0] || chat.preview_from || 'Chat';
    if (names.length) return names.length <= 4 ? names.join(', ') : `${names.slice(0, 3).join(', ')} + ${names.length - 3}`;
    if (chat.kind === 'meeting') return 'Meeting chat';
    return chat.preview_from || 'Group chat';
  }

  async function refreshTitle(chatId) {
    const chat = store.getChat(chatId);
    if (!chat) return;
    const title = computeTitle(chat);
    const names = (chat.members || []).map(member => personName(member.mri)).filter(Boolean).join(' ');
    await store.upsertChat(chatId, { title, search: `${title} ${names}`.toLowerCase().slice(0, 20000) });
  }

  // ----- chat list
  function chatPatch(conversation) {
    const thread = conversation.threadProperties || {};
    const props = conversation.properties || {};
    const last = conversation.lastMessage || {};
    const kind = chatKind(conversation.id);
    const horizon = parseTs(String(props.consumptionhorizon || '').split(';')[0]);
    const lastAt = parseTs(last.originalarrivaltime) || parseTs(last.composetime) || parseTs(props.lastimreceivedtime) || parseTs(last.id);
    let preview = '';
    if (last.messagetype && !isSkippedKind(last.messagetype)) {
      preview = systemLabel({ kind: last.messagetype, content: last.content || '' }, personName) || (last.messagetype === 'Text' ? decodeEntities(last.content || '') : stripHtml(last.content));
    }
    return {
      kind,
      topic: typeof thread.topic === 'string' ? thread.topic : '',
      preview: preview.slice(0, 200),
      preview_from: String(last.imdisplayname || ''),
      last_at: lastAt,
      last_msg_id: String(last.id || ''),
      last_updated_key: `${conversation.lastUpdatedMessageId ?? ''}:${conversation.lastUpdatedMessageVersion ?? ''}`,
      read_horizon: horizon,
      roster_version: String(thread.rosterVersion ?? ''),
      hidden: String(thread.hidden) === 'true',
      is_self: conversation.id === '48:notes'
    };
  }

  const wanted = id => { const kind = chatKind(id); return kind !== 'system' && kind !== 'channel'; };

  // Updates chat rows from a page of conversations; returns the chats that need their messages read again.
  async function applyConversations(conversations, { skipUnseen = false } = {}) {
    const dirty = [];
    for (const conversation of conversations) {
      if (!conversation?.id || !wanted(conversation.id)) continue;
      const patch = chatPatch(conversation);
      const before = store.getChat(conversation.id);
      const unseen = !before;
      const activity = unseen || before.last_msg_id !== patch.last_msg_id || before.last_updated_key !== patch.last_updated_key;
      if (unseen && patch.kind === 'oneOnOne') {
        patch.members = oneOnOneMris(conversation.id).map(mri => ({ mri, role: '' }));
        patch.member_count = patch.members.length;
      }
      const saved = await store.upsertChat(conversation.id, patch);
      if (unseen && patch.kind === 'oneOnOne') { await resolveMissing(saved.members.map(member => member.mri)); }
      if (unseen || (before.title === '' || before.title == null) || before.topic !== patch.topic) await refreshTitle(conversation.id);
      if (activity && !(unseen && skipUnseen)) dirty.push(conversation.id);
      else if (before && before.read_horizon !== patch.read_horizon) await recomputeUnread(conversation.id);
    }
    return dirty;
  }

  async function recomputeUnread(chatId) {
    const chat = store.getChat(chatId);
    if (!chat) return;
    let unread = Math.min(99, await store.countUnread(chatId, chat.read_horizon));
    if (!chat.read_horizon) {
      // Never opened: Teams flags only direct and group chats whose newest message is not ours.
      const [newest] = await store.latestMessages(chatId, 1);
      if (!['oneOnOne', 'group'].includes(chat.kind) || !newest || newest.mine) unread = 0;
    }
    await store.upsertChat(chatId, { unread });
  }

  // ----- messages
  async function absorb(chatId, rawMessages) {
    const messages = rawMessages.map(raw => normalizeMessage(raw, { chatId, myMri: me(), now: now() })).filter(Boolean);
    const { created, updated } = await store.upsertMessages(chatId, messages);
    for (const message of messages) {
      if (message.media.length) await media.register(chatId, message.msg_id, extractMedia(message.content));
      if (!message.deleted && message.files.length) await media.register(chatId, message.msg_id, message.files.filter(file => file.url).map(file => ({ url: file.url, key: mediaKey(file.url) })));
    }
    await learnPeople(messages);
    await resolveMissing(messages.flatMap(referencedMris));
    status.newMessages += created.length;
    return { messages, created, updated };
  }

  async function updateChatAfterMessages(chatId, messages, { created }) {
    const chat = store.getChat(chatId);
    const patch = { synced_at: now() };
    if (messages.length) {
      const timestamps = messages.map(message => message.ts).filter(Boolean);
      patch.newest_ts = Math.max(chat?.newest_ts || 0, ...timestamps);
      patch.oldest_ts = Math.min(chat?.oldest_ts || Infinity, ...timestamps);
    }
    if (created.length || !chat?.message_count) patch.message_count = await store.countMessages(chatId);
    await store.upsertChat(chatId, patch);
    await refreshTitle(chatId);
    await recomputeUnread(chatId);
  }

  // The newest messages of one chat: picks up new messages and edits, deletes and reactions.
  async function syncChatRecent(chatId, { pageSize = RECENT_MESSAGES, priority = PRIORITY.poll } = {}) {
    const page = await teams.messages(chatId, { pageSize, priority });
    const result = await absorb(chatId, page.messages);
    await updateChatAfterMessages(chatId, result.messages, result);
    await store.upsertChat(chatId, { rechecked_at: now() });
    return result;
  }

  // One message, read again after a change we made (edit, delete, reaction).
  async function refreshMessage(chatId, messageId) {
    let raw = null;
    try { raw = await teams.message(chatId, messageId); } catch (error) { if (error.code !== 'NOT_FOUND') throw error; }
    if (raw) {
      const result = await absorb(chatId, [raw]);
      if (result.created.length || result.updated.length) await updateChatAfterMessages(chatId, result.messages, result);
      return;
    }
    const row = await store.getMessage(chatId, messageId);
    if (row) await store.upsertMessages(chatId, [{ ...row, deleted: true, deleted_at: now(), content: '', media: [], search: '' }]);
  }

  // ----- steps
  async function pollOnce() {
    status.lastPollAt = now();
    try {
      const page = await teams.conversations({ pageSize: POLL_PAGE, priority: PRIORITY.poll });
      const dirty = await applyConversations(page.conversations);
      for (const chatId of dirty.slice(0, 8)) await syncChatRecent(chatId);
      // Chats that were not read this round stay marked by their old key, so the next round reads them.
      for (const chatId of dirty.slice(8)) await store.upsertChat(chatId, { last_updated_key: '' });
      if (now() - fullListAt > FULL_LIST_EVERY_MS) await fullChatList();
      await recheckStep();
      status.lastPollOk = now();
      status.lastPollError = '';
    } catch (error) {
      status.lastPollError = String(error.message || error).slice(0, 200);
      throw error;
    }
  }

  async function fullChatList() {
    let url;
    let pages = 0;
    const discovered = [];
    do {
      const page = await teams.conversations({ url, priority: PRIORITY.poll });
      discovered.push(...await applyConversations(page.conversations, { skipUnseen: true }));
      url = page.next;
      pages++;
      if (url) await sleep(300);
    } while (url && pages < 30);
    fullListAt = now();
    status.lastFullListAt = fullListAt;
    for (const chatId of discovered.slice(0, 20)) await syncChatRecent(chatId);
    for (const chatId of discovered.slice(20)) await store.upsertChat(chatId, { last_updated_key: '' });
    return pages;
  }

  // Edits and deletes do not always move a chat's last-message marker; a slow rotation re-reads recent messages of the busiest chats.
  async function recheckStep(count = RECHECK_CHATS_PER_POLL) {
    const recent = store.allChats().filter(chat => !chat.hidden && chat.kind !== 'system').slice(0, RECHECK_WINDOW);
    for (let index = 0; index < count && recent.length; index++) {
      const chat = recent[recheckCursor++ % recent.length];
      await syncChatRecent(chat.chat_id, { priority: PRIORITY.backfill });
    }
  }

  async function memberStep(chat) {
    if (chat.kind === 'oneOnOne' || chat.kind === 'self') return;
    const stale = !Array.isArray(chat.members) || chat.members.length === 0 || chat.roster_version !== chat.members_roster;
    if (!stale && chat.title) return;
    const { members, topic } = await teams.members(chat.chat_id, PRIORITY.backfill);
    const kept = members.slice(0, MAX_MEMBERS_KEPT);
    await store.upsertChat(chat.chat_id, { members: kept, member_count: members.length, members_roster: chat.roster_version, ...(topic && !chat.topic ? { topic } : {}) });
    if (members.length <= 12) await resolveMissing(kept.map(member => member.mri));
    await refreshTitle(chat.chat_id);
  }

  // One page of history for the next chat that is not fully copied. Returns false when everything is copied.
  async function backfillStep() {
    const pending = store.allChats().filter(chat => !chat.backfill_done && chat.kind !== 'system' && chat.kind !== 'channel');
    status.backfill.total = store.allChats().length;
    status.backfill.done = status.backfill.total - pending.length;
    const chat = pending.sort((a, b) => Number(!!a.hidden) - Number(!!b.hidden) || (b.last_at || 0) - (a.last_at || 0))[0];
    if (!chat) { status.backfill.current = ''; return false; }
    status.backfill.current = chat.title || chat.chat_id;
    await memberStep(chat).catch(error => { if (error.authFailure) throw error; log('warn', `Members of a chat could not be read: ${error.message}`); });
    const page = await teams.messages(chat.chat_id, { pageSize: BACKFILL_PAGE, url: chat.backfill_link || undefined, priority: PRIORITY.backfill });
    const result = await absorb(chat.chat_id, page.messages);
    await updateChatAfterMessages(chat.chat_id, result.messages, result);
    const finished = !page.next || page.messages.length === 0;
    await store.upsertChat(chat.chat_id, { backfill_link: finished ? '' : page.next, backfill_done: finished });
    return true;
  }

  async function mediaStep() {
    const result = await media.processPending(8);
    if (result.auth) await auth.refused();
    return result;
  }

  // ----- loops
  const jitter = ms => ms * (0.85 + random() * 0.3);
  async function loop(name, body, delay) {
    while (!stopped) {
      let wait = delay();
      try {
        if (!(await auth.ensure())) { status.paused = 'signed-out'; wait = Math.max(wait, 30000); } else { status.paused = ''; wait = await body() ?? wait; }
      } catch (error) {
        log('warn', `${name}: ${String(error.message || error).slice(0, 200)}`);
        wait = Math.max(wait, error.authFailure ? 30000 : 15000);
      }
      await sleep(wait);
    }
  }

  function start() {
    stopped = false;
    loop('poll', async () => { await pollOnce(); }, () => jitter(pollMs));
    loop('backfill', async () => { const more = await backfillStep(); return more ? backfillGapMs : 60000; }, () => backfillGapMs);
    loop('media', async () => { const result = await mediaStep(); return result.ok + result.failed ? 1500 : 5000; }, () => 5000);
  }

  return {
    start, stop: () => { stopped = true; }, status: () => ({ ...status, backfill: { ...status.backfill } }),
    pollOnce, fullChatList, syncChatRecent, refreshMessage, backfillStep, mediaStep, recheckStep, applyConversations, refreshTitle, recomputeUnread
  };
}
