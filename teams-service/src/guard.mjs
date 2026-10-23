// Keeps msteams-mcp's own session files read-only for this process.
//
// msteams-mcp's token helpers (refreshTokensViaHttp, getValidSubstrateToken,
// readSecure's plaintext migration, ...) write session-state.json and
// token-cache.json as a side effect. The MCP must stay the ONLY writer of
// those files: a stale overwrite of the session broke Teams once (PR #1014).
//
// installSessionGuard() replaces the writing functions of fs for paths inside
// the MCP state directory, before any library module is imported:
//   - writeFileSync / unlinkSync go to an in-memory overlay that reads in this
//     process then see, so the library's refresh logic works unchanged but its
//     rotated tokens exist only in this process's memory;
//   - every other way of changing those paths throws.
// Nothing outside the guarded directories is affected.
import fs from 'node:fs';
import path from 'node:path';
import { syncBuiltinESMExports } from 'node:module';

export class GuardedWriteError extends Error {
  constructor(operation, target) {
    super(`Refusing ${operation} on ${target}: the Teams service never writes msteams-mcp's session files`);
    this.name = 'GuardedWriteError';
    this.code = 'EGUARDED';
  }
}

const REAL = {
  readFileSync: fs.readFileSync, existsSync: fs.existsSync, writeFileSync: fs.writeFileSync, statSync: fs.statSync,
  unlinkSync: fs.unlinkSync, mkdirSync: fs.mkdirSync, chmodSync: fs.chmodSync
};
let installed = null;

function asPath(value) {
  if (typeof value === 'string') return path.resolve(value);
  if (value instanceof URL) return path.resolve(value.pathname);
  if (Buffer.isBuffer(value)) return path.resolve(value.toString());
  return null;
}

export function installSessionGuard(directories) {
  if (installed) return installed;
  const roots = [...new Set(directories.flatMap(dir => {
    const resolved = path.resolve(dir);
    const result = [resolved];
    try { result.push(fs.realpathSync(resolved)); } catch { /* directory may not exist */ }
    return result;
  }))];
  const guarded = value => {
    const target = asPath(value);
    return target && roots.some(root => target === root || target.startsWith(root + path.sep)) ? target : null;
  };
  // path -> string (written in memory) | null (deleted in memory)
  const overlay = new Map();
  const bornAt = new Map();

  fs.readFileSync = function (file, options) {
    const target = guarded(file);
    if (target && overlay.has(target)) {
      const value = overlay.get(target);
      if (value === null) throw Object.assign(new Error(`ENOENT: no such file or directory, open '${target}'`), { code: 'ENOENT' });
      const encoding = typeof options === 'string' ? options : options?.encoding;
      return encoding ? value : Buffer.from(value);
    }
    return REAL.readFileSync.apply(fs, arguments);
  };
  fs.existsSync = function (file) {
    const target = guarded(file);
    if (target && overlay.has(target)) return overlay.get(target) !== null;
    return REAL.existsSync.apply(fs, arguments);
  };
  fs.writeFileSync = function (file, data) {
    const target = guarded(file);
    if (!target) return REAL.writeFileSync.apply(fs, arguments);
    overlay.set(target, Buffer.isBuffer(data) ? data.toString('utf8') : String(data));
    bornAt.set(target, Date.now());
  };
  fs.unlinkSync = function (file) {
    const target = guarded(file);
    if (!target) return REAL.unlinkSync.apply(fs, arguments);
    overlay.set(target, null);
    bornAt.set(target, Date.now());
  };
  fs.mkdirSync = function (file) {
    if (guarded(file)) return undefined;
    return REAL.mkdirSync.apply(fs, arguments);
  };
  fs.chmodSync = function (file) {
    if (guarded(file)) return undefined;
    return REAL.chmodSync.apply(fs, arguments);
  };
  const twoPaths = new Set(['renameSync', 'copyFileSync', 'rename', 'copyFile', 'symlinkSync', 'linkSync', 'symlink', 'link']);
  const forbid = (owner, name) => {
    const original = owner[name];
    if (typeof original !== 'function') return;
    owner[name] = function (first, second) {
      const target = guarded(first) || (twoPaths.has(name) ? guarded(second) : null);
      if (target) throw new GuardedWriteError(name, target);
      return original.apply(owner, arguments);
    };
  };
  for (const name of ['appendFileSync', 'renameSync', 'copyFileSync', 'rmSync', 'rmdirSync', 'truncateSync', 'symlinkSync', 'linkSync',
                      'chownSync', 'utimesSync', 'createWriteStream', 'writeFile', 'appendFile', 'rename', 'copyFile', 'rm',
                      'unlink', 'truncate', 'mkdir', 'chmod']) forbid(fs, name);
  for (const name of ['writeFile', 'appendFile', 'rename', 'copyFile', 'rm', 'unlink', 'truncate', 'mkdir', 'chmod', 'rmdir', 'symlink', 'link', 'chown', 'utimes']) forbid(fs.promises, name);
  // Opening for writing would sidestep the checks above.
  const writeFlags = fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_APPEND | fs.constants.O_CREAT | fs.constants.O_TRUNC;
  for (const [owner, name] of [[fs, 'openSync'], [fs, 'open'], [fs.promises, 'open']]) {
    const original = owner[name];
    owner[name] = function (file, flags) {
      const target = guarded(file);
      if (target && typeof flags === 'string' && /[wa+]/.test(flags)) throw new GuardedWriteError(name, target);
      if (target && typeof flags === 'number' && (flags & writeFlags)) throw new GuardedWriteError(name, target);
      return original.apply(owner, arguments);
    };
  }
  syncBuiltinESMExports();

  installed = {
    roots,
    overlayPaths: () => [...overlay.keys()],
    hasOverlay: file => overlay.has(path.resolve(file)),
    // When the MCP (the file's only writer) wrote a newer file, or when our own
    // chain failed, the in-memory copies are dropped and reads go back to disk.
    dropOverlay() { overlay.clear(); bornAt.clear(); },
    // mtime of the file on disk, bypassing the overlay.
    diskMtimeMs(file) { try { return REAL.statSync(file).mtimeMs; } catch { return 0; } },
    oldestOverlayMs() { const times = [...bornAt.values()]; return times.length ? Math.min(...times) : null; }
  };
  return installed;
}
