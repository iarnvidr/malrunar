// Cached copies of images in messages. Teams serves them only with a valid
// token, so the bytes are fetched while the session works and kept on disk;
// old chats then still show them after a sign-out.
import fs from 'node:fs';
import path from 'node:path';
import { isTeamsMediaUrl } from './messages.mjs';
import { isTeamsFileUrl } from './files.mjs';

const MAX_BYTES = 30 * 1024 * 1024;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp']);

export function imageSize(buffer) {
  try {
    if (buffer.length > 24 && buffer.readUInt32BE(0) === 0x89504e47) return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
    if (buffer.length > 10 && buffer.toString('latin1', 0, 3) === 'GIF') return { width: buffer.readUInt16LE(6), height: buffer.readUInt16LE(8) };
    if (buffer.length > 30 && buffer.toString('latin1', 0, 4) === 'RIFF' && buffer.toString('latin1', 8, 12) === 'WEBP') {
      const kind = buffer.toString('latin1', 12, 16);
      if (kind === 'VP8X') return { width: 1 + buffer.readUIntLE(24, 3), height: 1 + buffer.readUIntLE(27, 3) };
      if (kind === 'VP8 ') return { width: buffer.readUInt16LE(26) & 0x3fff, height: buffer.readUInt16LE(28) & 0x3fff };
      if (kind === 'VP8L') { const bits = buffer.readUInt32LE(21); return { width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1 }; }
    }
    if (buffer.length > 4 && buffer[0] === 0xff && buffer[1] === 0xd8) {
      let offset = 2;
      while (offset + 9 < buffer.length) {
        if (buffer[offset] !== 0xff) { offset++; continue; }
        const marker = buffer[offset + 1];
        if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) };
        offset += 2 + buffer.readUInt16BE(offset + 2);
      }
    }
  } catch { /* fall through */ }
  return { width: 0, height: 0 };
}

export function createMediaCache({ dir, store, skypeToken, fileDownload, fetchImpl = fetch, now = () => Date.now(), log = () => {} }) {
  const fileFor = key => path.join(dir, key.slice(0, 2), key);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });

  async function register(chatId, msgId, items) {
    let added = 0;
    for (const item of items) {
      if (!isTeamsMediaUrl(item.url) && !isTeamsFileUrl(item.url)) continue;
      if (await store.getMedia(item.key)) continue;
      await store.saveMedia({ key: item.key, url: item.url, chat_id: chatId, msg_id: msgId, status: 'pending', attempts: 0, fetched_at: 0, size: 0, content_type: '', path: '' });
      added++;
    }
    return added;
  }

  // Returns 'auth' when Teams refused the token (the caller should stop and refresh).
  async function download(record) {
    const sharedFile = isTeamsFileUrl(record.url);
    const token = sharedFile ? null : skypeToken();
    if (!sharedFile && !token) return 'auth';
    let response;
    try {
      if (sharedFile) {
        if (!fileDownload) throw new Error('Shared file access is not ready');
        response = await fileDownload(record.url);
      } else {
        response = await fetchImpl(record.url, { headers: { Authorization: `skype_token ${token}` }, redirect: 'error', signal: AbortSignal.timeout(30000) });
      }
    } catch (error) {
      await store.saveMedia({ ...record, status: 'failed', attempts: (record.attempts || 0) + 1, error: 'The attachment could not be downloaded', fetched_at: now() });
      return error.authFailure ? 'auth' : 'failed';
    }
    if (response.status === 401 || response.status === 403) {
      // A revoked attachment must not block every later attachment forever.
      await store.saveMedia({ ...record, status: 'failed', attempts: (record.attempts || 0) + 1, error: `HTTP ${response.status}`, fetched_at: now() });
      return 'auth';
    }
    if (!response.ok) {
      const gone = response.status === 404 || response.status === 410;
      await store.saveMedia({ ...record, status: 'failed', attempts: gone ? 9 : (record.attempts || 0) + 1, error: `HTTP ${response.status}`, fetched_at: now() });
      return 'failed';
    }
    const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    const declared = Number(response.headers.get('content-length') || 0);
    if ((!sharedFile && !IMAGE_TYPES.has(type)) || declared > MAX_BYTES) {
      await store.saveMedia({ ...record, status: 'failed', attempts: 9, error: `not a cacheable image (${type || 'unknown type'}, ${declared} bytes)`, fetched_at: now() });
      return 'failed';
    }
    const chunks = [];
    let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > MAX_BYTES) break;
      chunks.push(chunk);
    }
    if (size > MAX_BYTES) {
      await store.saveMedia({ ...record, status: 'failed', attempts: 9, error: 'larger than 30 MB', fetched_at: now() });
      return 'failed';
    }
    const bytes = Buffer.concat(chunks);
    const target = fileFor(record.key);
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    const temporary = `${target}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, bytes, { mode: 0o600 });
    fs.renameSync(temporary, target);
    const { width, height } = imageSize(bytes);
    await store.saveMedia({ ...record, status: 'ok', content_type: type, size: bytes.length, path: path.relative(dir, target), width, height, error: '', fetched_at: now() });
    return 'ok';
  }

  async function processPending(limit = 10) {
    const result = { ok: 0, failed: 0, auth: false };
    for (const record of await store.pendingMedia(limit)) {
      const outcome = await download(record);
      if (outcome === 'auth') { result.auth = true; break; }
      result[outcome]++;
    }
    return result;
  }

  async function read(key) {
    if (!/^[0-9a-f]{32}$/.test(key)) return null;
    const record = await store.getMedia(key);
    if (!record || record.status !== 'ok') return null;
    const file = path.join(dir, record.path);
    if (!file.startsWith(dir + path.sep) || !fs.existsSync(file)) return null;
    return { file, contentType: record.content_type || 'application/octet-stream', size: record.size, attachment: isTeamsFileUrl(record.url) };
  }

  return { register, processPending, read, download, fileFor };
}
