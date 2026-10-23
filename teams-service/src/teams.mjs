// Calls to Microsoft Teams through msteams-mcp's own modules. Reads are paced
// by one shared queue so first-run history paging stays gentle; writes jump
// the queue. Nothing here queues a write for later: when the token is gone the
// call fails and says so.
export class TeamsError extends Error {
  constructor(message, code = 'UNKNOWN', { status, retryAfterMs } = {}) {
    super(message);
    this.name = 'TeamsError';
    this.code = code;
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
  get authFailure() { return this.code === 'AUTH_EXPIRED' || this.code === 'AUTH_REQUIRED'; }
}

export const PRIORITY = { write: 0, interactive: 1, poll: 2, backfill: 3 };

// One request at a time, a minimum gap between requests, interactive work first.
export function createLimiter({ gapMs = 250, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = () => Date.now() } = {}) {
  const queues = [[], [], [], []];
  let running = false;
  let lastEnd = 0;
  let pausedUntil = 0;
  async function pump() {
    if (running) return;
    running = true;
    try {
      for (;;) {
        const queue = queues.find(items => items.length);
        if (!queue) return;
        const job = queue.shift();
        const wait = Math.max(job.priority === PRIORITY.write ? 0 : pausedUntil - now(), lastEnd + gapMs - now());
        if (wait > 0) await sleep(wait);
        try { job.resolve(await job.fn()); } catch (error) { job.reject(error); }
        lastEnd = now();
      }
    } finally { running = false; }
  }
  return {
    run(priority, fn) {
      return new Promise((resolve, reject) => { queues[priority].push({ priority, fn, resolve, reject }); pump(); });
    },
    pauseFor(ms) { pausedUntil = Math.max(pausedUntil, now() + ms); },
    pending: () => queues.reduce((total, items) => total + items.length, 0)
  };
}

export function createTeams({ lib, auth, limiter, log = () => {}, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
  const { apiConfig, http, guards, chatsvc, profile } = lib;

  function session() {
    const result = guards.requireMessageAuthWithConfig();
    if (!result.ok) throw new TeamsError('Teams is signed out', 'AUTH_REQUIRED');
    return result.value;
  }

  const raise = error => {
    throw new TeamsError(String(error?.message || 'Teams request failed').slice(0, 300), String(error?.code || 'UNKNOWN'), { retryAfterMs: error?.retryAfterMs });
  };

  // Runs one Teams request with a fresh token; a refused token is renewed once.
  async function attempt(fn, priority) {
    if (!(await auth.ensure())) throw new TeamsError('Teams is signed out', 'AUTH_REQUIRED');
    for (let round = 0; ; round++) {
      let result;
      try {
        result = await limiter.run(priority, async () => fn(session()));
      } catch (error) { if (error instanceof TeamsError) throw error; throw new TeamsError(String(error.message).slice(0, 300), 'UNKNOWN'); }
      if (result.ok) return result.value;
      const code = String(result.error?.code || '');
      if ((code === 'AUTH_EXPIRED' || code === 'AUTH_REQUIRED') && round === 0) {
        if (await auth.refused()) continue;
        raise(result.error);
      }
      if (code === 'RATE_LIMITED' && priority >= PRIORITY.poll && round < 3) {
        const wait = Math.min(result.error.retryAfterMs || 30000, 5 * 60 * 1000);
        limiter.pauseFor(wait);
        log('warn', `Rate limited by Teams; pausing background requests for ${Math.round(wait / 1000)} s`);
        await sleep(wait);
        continue;
      }
      raise(result.error);
    }
  }

  const getJson = (url, priority) => attempt(({ auth: tokens, baseUrl }) =>
    http.httpRequest(url, { method: 'GET', headers: apiConfig.getSkypeAuthHeaders(tokens.skypeToken, tokens.authToken, baseUrl) }), priority);

  return {
    me: () => session().auth.userMri,

    // ----- reads
    async conversations({ pageSize = 200, url, priority = PRIORITY.poll } = {}) {
      const target = url || (({ region, baseUrl }) => `${baseUrl}/api/chatsvc/${region}/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=${pageSize}`)(session());
      const response = await getJson(target, priority);
      const data = response.data || {};
      return { conversations: Array.isArray(data.conversations) ? data.conversations : [], next: data._metadata?.backwardLink || '', totalCount: data._metadata?.totalCount };
    },

    async messages(chatId, { pageSize = 100, url, priority = PRIORITY.poll } = {}) {
      const target = url || (({ region, baseUrl }) =>
        `${apiConfig.CHATSVC_API.messages(region, chatId, undefined, baseUrl)}?view=msnp24Equivalent|supportsMessageProperties&pageSize=${pageSize}`)(session());
      const response = await getJson(target, priority);
      const data = response.data || {};
      return { messages: Array.isArray(data.messages) ? data.messages : [], next: data._metadata?.backwardLink || '' };
    },

    async message(chatId, messageId, priority = PRIORITY.interactive) {
      const { region, baseUrl } = session();
      const response = await getJson(apiConfig.CHATSVC_API.singleMessage(region, chatId, messageId, baseUrl), priority);
      return response.data || null;
    },

    async members(chatId, priority = PRIORITY.backfill) {
      const { region, baseUrl } = session();
      const response = await getJson(`${baseUrl}/api/chatsvc/${region}/v1/threads/${encodeURIComponent(chatId)}?view=msnp24Equivalent`, priority);
      const data = response.data || {};
      return {
        members: (Array.isArray(data.members) ? data.members : []).map(member => ({ mri: String(member.id || ''), role: String(member.role || '') })).filter(member => member.mri),
        topic: typeof data.properties?.topic === 'string' ? data.properties.topic : ''
      };
    },

    async names(mris) {
      if (!mris.length) return [];
      if (!(await auth.ensure())) return [];
      const result = await limiter.run(PRIORITY.backfill, () => profile.resolveProfiles(mris));
      if (!result.ok) return [];
      return [...result.value.profiles.values()].map(entry => ({ mri: entry.mri, name: entry.displayName || '', email: entry.email || '' }));
    },

    skypeToken: () => { try { return session().auth.skypeToken; } catch { return null; } },

    // ----- writes (live only; nothing is queued)
    send: (chatId, text, { html = false, files = [] } = {}) => attempt(({ auth: tokens, region, baseUrl }) => {
      if (!files.length) return chatsvc.sendMessage(chatId, text, { contentType: html ? 'html' : 'text' });
      return http.httpRequest(apiConfig.CHATSVC_API.messages(region, chatId, undefined, baseUrl), {
        method: 'POST', headers: apiConfig.getMessagingHeaders(tokens.skypeToken, tokens.authToken, baseUrl),
        body: JSON.stringify({ content: text, messagetype: 'RichText/Html', contenttype: 'text',
          imdisplayname: lib.tokenExtractor.getUserDisplayName() || 'User', clientmessageid: `${Date.now()}${Math.floor(Math.random() * 1e6).toString().padStart(6, '0')}`,
          properties: { files: JSON.stringify(files.map(file => ({ id: file.id, fileName: file.name, fileType: file.type, objectUrl: file.url, fileInfo: { shareUrl: file.url } }))) } })
      });
    }, PRIORITY.write),
    edit: (chatId, messageId, text) => attempt(() => chatsvc.editMessage(chatId, messageId, text, 'text'), PRIORITY.write),

    remove: (chatId, messageId) => attempt(() => chatsvc.deleteMessage(chatId, messageId), PRIORITY.write),
    react: (chatId, messageId, key) => attempt(() => chatsvc.addReaction(chatId, messageId, key), PRIORITY.write),
    unreact: (chatId, messageId, key) => attempt(() => chatsvc.removeReaction(chatId, messageId, key), PRIORITY.write),
    markRead: (chatId, messageId) => attempt(() => chatsvc.markAsRead(chatId, messageId), PRIORITY.write)
  };
}
