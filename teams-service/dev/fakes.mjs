// A scripted Teams world for sync and server tests.
import { TeamsError } from '../src/teams.mjs';

export const ME = '8:orgid:11111111-1111-1111-1111-111111111111';
export const ANNA = '8:orgid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
export const ONE_TO_ONE = `19:${ANNA.slice(8)}_${ME.slice(8)}@unq.gbl.spaces`;
export const GROUP = '19:group1@thread.v2';
export const MEETING = '19:meeting_xyz@thread.v2';
export const SELF = '48:notes';

export const from = mri => `https://emea.ng.msg.teams.microsoft.com/v1/users/ME/contacts/${mri}`;

export function raw(id, content, overrides = {}) {
  const iso = new Date(Number(id)).toISOString().replace('Z', '0000Z');
  return { id: String(id), messagetype: 'RichText/Html', content, from: from(ANNA), imdisplayname: 'Anna', originalarrivaltime: iso, composetime: iso, version: String(id), properties: {}, ...overrides };
}

export function conversation(id, lastMessage, extra = {}) {
  return {
    id, version: 1, properties: { consumptionhorizon: '0;0;0', ...(extra.properties || {}) }, threadProperties: { topic: extra.topic || '', rosterVersion: 5, ...(extra.threadProperties || {}) },
    lastMessage: lastMessage ? { id: lastMessage.id, messagetype: lastMessage.messagetype, content: lastMessage.content, imdisplayname: lastMessage.imdisplayname, originalarrivaltime: lastMessage.originalarrivaltime, from: lastMessage.from } : undefined,
    lastUpdatedMessageId: extra.updatedId ?? lastMessage?.id, lastUpdatedMessageVersion: extra.updatedVersion ?? lastMessage?.version
  };
}

export class FakeWorld {
  constructor() {
    this.chats = new Map();      // id -> {conversation fields, messages: raw[] newest first}
    this.calls = [];
    this.signedOut = false;
    this.members = new Map();
    this.names = new Map([[ANNA, 'Anna Example'], [ME, 'Alex Example']]);
  }
  addChat(id, messages, extra = {}) { this.chats.set(id, { extra, messages: [...messages].sort((a, b) => Number(b.id) - Number(a.id)) }); }
  conversationsList() {
    return [...this.chats].map(([id, chat]) => conversation(id, chat.messages[0], chat.extra))
      .sort((a, b) => Number(b.lastMessage?.id || 0) - Number(a.lastMessage?.id || 0));
  }
  teams() {
    const world = this;
    const guard = () => { if (world.signedOut) throw new TeamsError('Teams is signed out', 'AUTH_REQUIRED'); };
    return {
      me: () => ME,
      skypeToken: () => (world.signedOut ? null : 'skype-token'),
      async conversations({ pageSize = 200, url } = {}) {
        guard();
        world.calls.push(['conversations', pageSize, url || '']);
        const all = world.conversationsList();
        const offset = url ? Number(url.split(':')[1]) : 0;
        const size = url ? 2 : pageSize;
        const page = all.slice(offset, offset + size);
        const more = offset + size < all.length && (url || pageSize >= 200);
        return { conversations: page, next: more ? `conv:${offset + size}` : '' };
      },
      async messages(chatId, { pageSize = 100, url } = {}) {
        guard();
        world.calls.push(['messages', chatId, pageSize, url || '']);
        const chat = world.chats.get(chatId);
        const offset = url ? Number(url.split(':')[2]) : 0;
        const page = chat.messages.slice(offset, offset + pageSize);
        const more = offset + pageSize < chat.messages.length;
        return { messages: page, next: more ? `msgs:${chatId}:${offset + pageSize}` : '' };
      },
      async message(chatId, messageId) {
        guard();
        world.calls.push(['message', chatId, messageId]);
        const found = world.chats.get(chatId).messages.find(message => message.id === messageId);
        if (!found) throw new TeamsError('not found', 'NOT_FOUND');
        return found;
      },
      async members(chatId) {
        guard();
        world.calls.push(['members', chatId]);
        return { members: world.members.get(chatId) || [{ mri: ME, role: 'Admin' }, { mri: ANNA, role: 'User' }], topic: '' };
      },
      async names(mris) { guard(); world.calls.push(['names', mris.length]); return mris.filter(mri => world.names.has(mri)).map(mri => ({ mri, name: world.names.get(mri), email: '' })); },
      async send(chatId, text, options = {}) {
        guard();
        const id = Date.now() + world.chats.get(chatId).messages.length + 1;
        const properties = options.files?.length ? { files: JSON.stringify(options.files.map(file => ({ id: file.id, fileName: file.name, fileType: file.type, objectUrl: file.url, fileInfo: { shareUrl: file.url } }))) } : {};
        world.chats.get(chatId).messages.unshift(raw(id, text, { messagetype: options.html ? 'RichText/Html' : 'Text', from: from(ME), imdisplayname: 'Alex Example', properties }));
        world.calls.push(['send', chatId, text, options.html ? 'html' : 'text', options.files || []]);
        return { messageId: String(id) };
      },
      async edit(chatId, messageId, text) {
        guard();
        const message = world.chats.get(chatId).messages.find(item => item.id === messageId);
        message.content = text;
        message.properties = { ...message.properties, edittime: String(Date.now()) };
        message.version = String(Number(message.version) + 1);
        world.calls.push(['edit', chatId, messageId, text]);
      },
      async remove(chatId, messageId) {
        guard();
        const message = world.chats.get(chatId).messages.find(item => item.id === messageId);
        message.content = '';
        message.properties = { ...message.properties, deletetime: String(Date.now()) };
        message.version = String(Number(message.version) + 1);
        world.calls.push(['remove', chatId, messageId]);
      },
      async react(chatId, messageId, key) {
        guard();
        const message = world.chats.get(chatId).messages.find(item => item.id === messageId);
        message.properties = { ...message.properties, emotions: JSON.stringify([{ key, users: [{ mri: ME, time: 1 }] }]) };
        message.version = String(Number(message.version) + 1);
        world.calls.push(['react', chatId, messageId, key]);
      },
      async unreact(chatId, messageId, key) {
        guard();
        const message = world.chats.get(chatId).messages.find(item => item.id === messageId);
        message.properties = { ...message.properties, emotions: '[]' };
        message.version = String(Number(message.version) + 1);
        world.calls.push(['unreact', chatId, messageId, key]);
      },
      async markRead(chatId, messageId) { guard(); world.calls.push(['markRead', chatId, messageId]); }
    };
  }
}

export const fakeAuth = (state = { haveTokens: true, state: 'ok' }) => ({
  ensure: async () => state.state !== 'signed-out', refused: async () => state.state !== 'signed-out', status: () => ({ ...state }), snapshot: () => ({ ok: true })
});

export const fakeMedia = () => {
  const registered = [];
  return { registered, register: async (chatId, msgId, items) => { for (const item of items) if (!registered.includes(item.key)) registered.push(item.key); return items.length; }, processPending: async () => ({ ok: 0, failed: 0, auth: false }), read: async () => null };
};
