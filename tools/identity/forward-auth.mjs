// vendored copy - edit tools/identity, run scripts/sync-identity.sh
// Forward-auth identity: who is the user, according to a trusted reverse proxy (Node, no dependencies).
// JavaScript port of forward_auth.py with the same settings, trust rule and test vectors.
//
// Settings (read with the component's own prefix, e.g. MALRUNAR_TEAMS_SERVICE_):
//   <PFX>USER_HEADER (older name: IDENTITY_HEADER), GROUPS_HEADER, NAME_HEADER, EMAIL_HEADER:
//       header names, defaults Remote-User, Remote-Groups (comma-separated), Remote-Name, Remote-Email
//   <PFX>TRUSTED_PROXIES      comma-separated CIDRs (IPv4 and IPv6) the peer address must be in
//   <PFX>PROXY_SECRET_HEADER  header in which the proxy sends a shared secret
//   <PFX>PROXY_SECRET_FILE    one secret per line, at most two (old and new during a rotation); blank
//                             lines and lines starting with # ignored; read again when its mtime changes
//
// Trust rule: neither proxies nor secret configured: nothing is trusted, everyone is anonymous
// (startupWarning() has the text to log). Only one configured: it must pass. BOTH configured: BOTH must
// pass. An untrusted request yields the anonymous identity: its identity headers are ignored.
import { createHash, timingSafeEqual } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';
import { BlockList, isIP } from 'node:net';

export const DEFAULTS = Object.freeze({ userHeader: 'Remote-User', groupsHeader: 'Remote-Groups', nameHeader: 'Remote-Name', emailHeader: 'Remote-Email' });
export const MAX_VALUE_LENGTH = 256;
export const MAX_GROUPS_LENGTH = 4096;
export const MAX_GROUPS = 128;
export const MIN_SECRET_LENGTH = 16;
export const MAX_SECRETS = 2;

export class ConfigError extends Error {
  constructor(message) { super(message); this.name = 'ConfigError'; }
}

const identity = (fields = {}) => Object.freeze({
  user: null, name: null, email: null, trusted: false, ...fields,
  groups: Object.freeze([...(fields.groups || [])]),
  get anonymous() { return this.user === null; },
});
export const ANONYMOUS = identity();

const CONTROL = /[\u0000-\u001f\u007f-\u009f\u{2028}\u{2029}]/u;
const clean = (value, limit = MAX_VALUE_LENGTH) => {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text || [...text].length > limit || CONTROL.test(text)) return null;
  return text;
};
const parseGroups = value => {
  const text = clean(value, MAX_GROUPS_LENGTH);
  if (text === null) return [];
  const groups = [];
  for (let part of text.split(',')) {
    part = part.trim();
    if (!part) continue;
    if ([...part].length > MAX_VALUE_LENGTH) return [];
    if (!groups.includes(part)) groups.push(part);
  }
  return groups.length <= MAX_GROUPS ? groups : [];
};
const headerName = (name, setting) => {
  const text = String(name ?? '').trim();
  if (!/^[A-Za-z0-9-]+$/.test(text)) throw new ConfigError(`${setting} must be a header name (letters, digits, hyphen)`);
  return text;
};
const digest = value => createHash('sha256').update(value, 'utf8').digest();

// {family, address} of a peer address (IPv4-mapped IPv6 counts as IPv4), or null.
const parsePeer = peer => {
  if (typeof peer !== 'string') return null;
  const text = peer.trim().split('%')[0];
  const family = isIP(text);
  if (!family) return null;
  const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(text);
  if (family === 6 && mapped && isIP(mapped[1]) === 4) return { family: 4, address: mapped[1] };
  if (family === 6 && /^::ffff:[0-9a-f]{1,4}:[0-9a-f]{1,4}$/i.test(text)) {
    const [high, low] = text.slice(7).split(':').map(part => parseInt(part, 16));
    return { family: 4, address: `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}` };
  }
  return { family: family, address: text };
};

const readSecrets = (path, setting) => {
  let lines;
  try { lines = readFileSync(path, 'utf8').split(/\r?\n/); }
  catch (error) { throw new ConfigError(`${setting}: cannot read ${path}: ${error.code || error.name}`); }
  const secrets = lines.map(line => line.trim()).filter(line => line && !line.startsWith('#'));
  if (!secrets.length) throw new ConfigError(`${setting}: ${path} holds no secret`);
  if (secrets.length > MAX_SECRETS) throw new ConfigError(`${setting}: ${path} holds more than ${MAX_SECRETS} secrets`);
  if (secrets.some(secret => secret.length < MIN_SECRET_LENGTH)) throw new ConfigError(`${setting}: a secret is shorter than ${MIN_SECRET_LENGTH} characters`);
  return secrets.map(digest);
};

const statStamp = path => {
  try { const info = statSync(path); return `${info.mtimeMs}:${info.size}`; } catch { return null; }
};

export class IdentityConfig {
  constructor({ userHeader = DEFAULTS.userHeader, groupsHeader = DEFAULTS.groupsHeader, nameHeader = DEFAULTS.nameHeader,
    emailHeader = DEFAULTS.emailHeader, trustedProxies = [], secretHeader = null, secretFile = null, prefix = '' } = {}) {
    this.userHeader = headerName(userHeader, `${prefix}USER_HEADER`);
    this.groupsHeader = headerName(groupsHeader, `${prefix}GROUPS_HEADER`);
    this.nameHeader = headerName(nameHeader, `${prefix}NAME_HEADER`);
    this.emailHeader = headerName(emailHeader, `${prefix}EMAIL_HEADER`);
    this.trustedProxies = Object.freeze([...trustedProxies]);
    // One list per family: node would otherwise match an IPv4 address against an IPv6 network (::/0).
    this._blocks = { 4: new BlockList(), 6: new BlockList() };
    for (const item of this.trustedProxies) {
      const [address, bits, extra] = String(item).split('/');
      const family = isIP(address);
      const max = family === 4 ? 32 : 128;
      const prefixLength = bits === undefined ? max : (/^\d+$/.test(bits) ? Number(bits) : NaN);
      if (!family || extra !== undefined || !(prefixLength >= 0 && prefixLength <= max)) throw new ConfigError(`${prefix}TRUSTED_PROXIES: '${item}' is not an address or CIDR`);
      this._blocks[family].addSubnet(address, prefixLength, family === 4 ? 'ipv4' : 'ipv6');
    }
    this.secretHeader = secretHeader ? headerName(secretHeader, `${prefix}PROXY_SECRET_HEADER`) : null;
    this.secretFile = secretFile || null;
    if (Boolean(this.secretHeader) !== Boolean(this.secretFile)) throw new ConfigError(`${prefix}PROXY_SECRET_HEADER and ${prefix}PROXY_SECRET_FILE go together: set both or neither`);
    this._setting = `${prefix}PROXY_SECRET_FILE`;
    this._digests = [];
    this._stamp = null;
    if (this.secretFile) { this._digests = readSecrets(this.secretFile, this._setting); this._stamp = statStamp(this.secretFile); }
  }

  // Unprefixed settings: { USER_HEADER, GROUPS_HEADER, ..., TRUSTED_PROXIES: 'a, b' or [..], PROXY_SECRET_FILE }.
  static fromObject(values = {}, prefix = '') {
    const get = name => {
      const value = values[name];
      if (value === undefined || value === null) return '';
      return Array.isArray(value) ? value.join(',') : String(value).trim();
    };
    return new IdentityConfig({
      userHeader: get('USER_HEADER') || get('IDENTITY_HEADER') || DEFAULTS.userHeader,
      groupsHeader: get('GROUPS_HEADER') || DEFAULTS.groupsHeader,
      nameHeader: get('NAME_HEADER') || DEFAULTS.nameHeader,
      emailHeader: get('EMAIL_HEADER') || DEFAULTS.emailHeader,
      trustedProxies: get('TRUSTED_PROXIES').split(',').map(item => item.trim()).filter(Boolean),
      secretHeader: get('PROXY_SECRET_HEADER') || null,
      secretFile: get('PROXY_SECRET_FILE') || null,
      prefix,
    });
  }

  static fromEnv(env, prefix) {
    const keys = ['USER_HEADER', 'IDENTITY_HEADER', 'GROUPS_HEADER', 'NAME_HEADER', 'EMAIL_HEADER', 'TRUSTED_PROXIES', 'PROXY_SECRET_HEADER', 'PROXY_SECRET_FILE'];
    return IdentityConfig.fromObject(Object.fromEntries(keys.filter(key => (prefix + key) in env).map(key => [key, env[prefix + key]])), prefix);
  }

  // 'none' (nothing trusted), 'proxies', 'secret' or 'both' (both must pass).
  get mode() {
    if (this.trustedProxies.length && this.secretHeader) return 'both';
    if (this.trustedProxies.length) return 'proxies';
    return this.secretHeader ? 'secret' : 'none';
  }

  startupWarning() {
    if (this.mode !== 'none') return null;
    return `forward-auth: neither trusted proxies nor a proxy secret is configured, so no identity header (${this.userHeader}, ${this.groupsHeader}, ${this.nameHeader}, ${this.emailHeader}) is trusted and every request is anonymous`;
  }

  toJSON() { return { mode: this.mode, userHeader: this.userHeader, proxies: this.trustedProxies.length, secretHeader: this.secretHeader }; }

  _secrets() {
    if (!this.secretFile) return [];
    const stamp = statStamp(this.secretFile);
    if (stamp !== null && stamp !== this._stamp) {
      try { this._digests = readSecrets(this.secretFile, this._setting); } catch { /* keep the last good secrets */ }
      this._stamp = stamp;
    }
    return this._digests;
  }

  // Lower-case names of the headers that must never be forwarded or logged.
  sensitiveHeaders() {
    const names = [this.userHeader, this.groupsHeader, this.nameHeader, this.emailHeader, ...(this.secretHeader ? [this.secretHeader] : [])];
    return new Set(names.map(name => name.toLowerCase()));
  }

  // A copy of headers without the secret and the identity headers.
  scrubHeaders(headers) {
    const sensitive = this.sensitiveHeaders();
    return Object.fromEntries(Object.entries(headers || {}).filter(([name]) => !sensitive.has(name.toLowerCase())));
  }

  _trusted(peerIp, lookup) {
    if (this.mode === 'none') return false;
    let proxyOk = true;
    if (this.trustedProxies.length) {
      const peer = parsePeer(peerIp);
      proxyOk = peer !== null && this._blocks[peer.family].check(peer.address, peer.family === 4 ? 'ipv4' : 'ipv6');
    }
    let secretOk = true;
    if (this.secretHeader) {
      const supplied = lookup.get(this.secretHeader.toLowerCase());
      secretOk = false;
      if (typeof supplied === 'string' && supplied.trim()) {
        const given = digest(supplied.trim());
        for (const known of this._secrets()) if (timingSafeEqual(given, known)) secretOk = true; // every secret is compared
      }
    }
    return proxyOk && secretOk;
  }

  // The identity of a request from peerIp (req.socket.remoteAddress) with headers (req.headers or any
  // object; names are case-insensitive).
  resolve(peerIp, headers) {
    const lookup = new Map(Object.entries(headers || {}).map(([name, value]) => [name.toLowerCase(), Array.isArray(value) ? value.join(', ') : value]));
    if (!this._trusted(peerIp, lookup)) return ANONYMOUS;
    return identity({
      user: clean(lookup.get(this.userHeader.toLowerCase())),
      name: clean(lookup.get(this.nameHeader.toLowerCase())),
      email: clean(lookup.get(this.emailHeader.toLowerCase())),
      groups: parseGroups(lookup.get(this.groupsHeader.toLowerCase())),
      trusted: true,
    });
  }
}
