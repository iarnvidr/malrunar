// Shared files use the Graph access already held by the Teams session. The
// preauthenticated download URL stays in memory and is never stored or logged.
import { TeamsError } from './teams.mjs';
import crypto from 'node:crypto';

export function isTeamsFileUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && /(^|\.)sharepoint(?:-df)?\.com$/i.test(url.hostname);
  } catch { return false; }
}

export function createFileClient({ token, fetchImpl = fetch }) {
  async function graph(path, options = {}) {
    const access = await token();
    if (!access) throw new TeamsError('Teams is signed out', 'AUTH_REQUIRED');
    const response = await fetchImpl(`https://graph.microsoft.com/v1.0${path}`, {
      ...options, headers: { Authorization: `Bearer ${access}`, ...options.headers },
      redirect: 'error', signal: AbortSignal.timeout(60000)
    });
    if (!response.ok) throw new TeamsError(`File request failed (HTTP ${response.status})`,
      response.status === 401 ? 'AUTH_EXPIRED' : 'UPSTREAM', { status: response.status });
    return response;
  }

  async function download(url) {
    if (!isTeamsFileUrl(url)) throw new Error('Unsupported shared file');
    const share = `u!${Buffer.from(url).toString('base64url')}`;
    // Graph generates the download annotations on the default projection;
    // selecting them explicitly omits them on this tenant.
    const response = await graph(`/shares/${share}/driveItem`);
    const item = await response.json();
    const target = new URL(item['@microsoft.graph.downloadUrl'] || 'https://invalid.invalid');
    if (target.protocol !== 'https:' || !/(^|\.)(sharepoint(?:-df)?\.com|1drv\.com|storage\.live\.com)$/i.test(target.hostname)) {
      throw new Error('No supported download URL for this file');
    }
    if (Number(item.size) > 30 * 1024 * 1024) throw new Error('The shared file exceeds the 30 MB archive limit');
    // No bearer token is sent to the content host.
    return fetchImpl(target, { redirect: 'error', signal: AbortSignal.timeout(60000) });
  }
  async function upload({ bytes, name, type, memberMris = [] }) {
    const folderName = 'Microsoft Teams Chat Files';
    try { await graph(`/me/drive/root:/${encodeURIComponent(folderName)}`); }
    catch (error) {
      if (error.status !== 404) throw error;
      await graph('/me/drive/root/children', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: folderName, folder: {}, '@microsoft.graph.conflictBehavior': 'fail' }) });
    }
    const uniqueName = `${crypto.randomBytes(6).toString('hex')}-${name}`;
    const item = await (await graph(`/me/drive/root:/${encodeURIComponent(folderName)}/${encodeURIComponent(uniqueName)}:/content`, {
      method: 'PUT', headers: { 'Content-Type': type || 'application/octet-stream' }, body: bytes
    })).json();
    if (!item.id || !isTeamsFileUrl(item.webUrl)) throw new TeamsError('The uploaded file has no usable link', 'UPSTREAM');
    const recipients = [...new Set(memberMris)].map(mri => /^8:orgid:([a-f0-9-]{36})$/i.exec(mri)?.[1]).filter(Boolean).map(objectId => ({ objectId }));
    if (recipients.length) {
      const shared = await (await graph(`/me/drive/items/${encodeURIComponent(item.id)}/invite`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ recipients, requireSignIn: true, sendInvitation: false, roles: ['read'] })
      })).json();
      if (shared.value?.some(entry => entry.error)) throw new TeamsError('Some chat members could not be given access to the file', 'UPSTREAM');
    }
    return { id: item.id, url: item.webUrl, name, type: name.split('.').pop()?.toLowerCase() || '', size: bytes.length };
  }
  return { download, upload, graph };
}
