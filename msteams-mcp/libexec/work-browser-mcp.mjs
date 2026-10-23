#!/usr/bin/env node
// A stdio MCP server that looks at and drives the VISIBLE Firefox of the work-browser component. A
// fresh Playwright context cannot attach to Firefox's locked, signed-in profile, so this controls the
// browser windows on the shared X display, and the agent and the person see the same tabs and login
// state. Page text is read through WebDriver BiDi (Firefox started with --remote-debugging-port, see
// the README) or, optionally, through the Tail extension of the tail-browser-mcp component.
//
// Settings (all optional): MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR (the profile of the browser whose
// windows are exposed; set by malrunar-msteams-mcp), MALRUNAR_MSTEAMS_MCP_BROWSER_MCP_BACKEND
// (`bidi`, the default, or `tail`), MALRUNAR_MSTEAMS_MCP_BIDI_URL (default ws://127.0.0.1:9222/session),
// MALRUNAR_MSTEAMS_MCP_TAIL_CLIENT (default /opt/malrunar/tail-browser-mcp/client.mjs),
// MALRUNAR_MSTEAMS_MCP_DOWNLOADS_DIR (default ~/Downloads).
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';

const run = (cmd, args, options = {}) => {
  const result = spawnSync(cmd, args, { encoding: 'utf8', timeout: 15000, maxBuffer: 8 * 1024 * 1024, ...options });
  if (result.error || result.status !== 0) throw new Error(`${cmd} failed: ${(result.stderr || result.error?.message || '').trim()}`);
  return result.stdout.trim();
};

function display() {
  if (process.env.DISPLAY) {
    try { run('xdpyinfo', []); return; } catch { /* Find the current xrdp display. */ }
  }
  const sockets = run('sh', ['-c', 'ls -t /tmp/.X11-unix/X[0-9]* 2>/dev/null || true']).split('\n');
  for (const socket of sockets) {
    if (!socket) continue;
    process.env.DISPLAY = `:${socket.split('X').pop()}`;
    try { run('xdpyinfo', []); return; } catch { /* Try the next display. */ }
  }
  throw new Error('No reachable desktop display. Sign in to the desktop session first.');
}

function windows() {
  display();
  const result = spawnSync('xdotool', ['search', '--classname', 'Navigator'], { encoding: 'utf8' });
  if (result.status !== 0) return [];
  return result.stdout.trim().split('\n').filter(Boolean).filter(id => {
    try {
      if (!/WM_WINDOW_ROLE.*"browser"/.test(run('xprop', ['-id', id, 'WM_WINDOW_ROLE']))) return false;
      // Other Firefox windows may share the X display with a different profile. Only expose
      // windows that belong to the visible browser's own profile.
      const pid = run('xdotool', ['getwindowpid', id]);
      if (!/^\d+$/.test(pid)) return false;
      const command = readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0');
      const profileIndex = command.indexOf('-profile');
      const profile = process.env.MALRUNAR_MSTEAMS_MCP_BROWSER_PROFILE_DIR || join(homedir(), '.config', 'malrunar-work-browser', 'profile');
      return profileIndex >= 0 && command[profileIndex + 1] === profile;
    }
    catch { return false; }
  }).map(id => ({ id, title: run('xdotool', ['getwindowname', id]) }));
}

function windowFor(args) {
  const choices = windows();
  if (!choices.length) throw new Error('No visible Firefox browser window.');
  const choice = args.window_id ? choices.find(w => w.id === String(args.window_id)) : choices[0];
  if (!choice) throw new Error('That window is not a visible Firefox browser window. Call browser_windows again.');
  return choice;
}

function focus(id) { run('xdotool', ['windowactivate', '--sync', id]); }

// Two backends read DOM text without changing selection or focus; each call owns a short-lived
// protocol session: WebDriver BiDi (default) and Tail's browser extension.
const BIDI_URL = process.env.MALRUNAR_MSTEAMS_MCP_BIDI_URL || 'ws://127.0.0.1:9222/session';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EXTENSION_BROWSER = (process.env.MALRUNAR_MSTEAMS_MCP_BROWSER_MCP_BACKEND || 'bidi') === 'tail';

async function bidi(fn) {
  if (EXTENSION_BROWSER) {
    const { connection } = await import(process.env.MALRUNAR_MSTEAMS_MCP_TAIL_CLIENT || '/opt/malrunar/tail-browser-mcp/client.mjs');
    return connection(fn);
  }
  for (let attempt = 0; ; attempt++) {
    const ws = new WebSocket(BIDI_URL);
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('BiDi connect timeout')), 5000);
      ws.onopen = () => { clearTimeout(t); resolve(); };
      ws.onerror = () => { clearTimeout(t); reject(new Error('Firefox BiDi is not reachable')); };
    });
    let next = 0;
    const pending = new Map();
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      const p = m.id !== undefined && pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      if (m.type === 'error') p.reject(new Error(`${m.error}: ${m.message}`)); else p.resolve(m.result);
    };
    const send = (method, params = {}, timeoutMs = 20000) => new Promise((resolve, reject) => {
      const id = ++next;
      const t = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, timeoutMs);
      pending.set(id, { resolve: v => { clearTimeout(t); resolve(v); }, reject: e => { clearTimeout(t); reject(e); } });
      ws.send(JSON.stringify({ id, method, params }));
    });
    try {
      await send('session.new', { capabilities: {} });
    } catch (error) {
      ws.close();
      // Another caller's session may still be open; they are short-lived.
      if (attempt < 5 && /session/i.test(error.message)) { await sleep(700); continue; }
      throw error;
    }
    try { return await fn(send); }
    finally {
      await send('session.end', {}, 3000).catch(() => {});
      ws.close();
    }
  }
}

async function evaluate(send, context, expression) {
  const r = await send('script.evaluate', { expression, target: { context }, awaitPromise: true, resultOwnership: 'none' });
  if (r.type === 'exception') throw new Error(r.exceptionDetails?.text || 'page script failed');
  return r.result?.value;
}

// The X11 window title is "<active tab's document.title> — <brand>", so match
// the active (visible) top-level tab whose title equals the window's.
async function contextForWindow(send, windowTitle) {
  const want = windowTitle.includes(' — ') ? windowTitle.replace(/ — [^—]*$/, '') : '';
  // A generic Firefox window title must not select an empty background tab.
  const { contexts } = await send('browsingContext.getTree', { maxDepth: 0 });
  if (EXTENSION_BROWSER) {
    const pick = contexts.find(c => want && c.title === want && c.active)
      || contexts.find(c => want && c.title === want)
      || (contexts.filter(c => c.active).length === 1 ? contexts.find(c => c.active) : null);
    if (!pick) throw new Error('Could not match this window to an extension tab');
    return pick;
  }
  const infos = [];
  for (const c of contexts) {
    try {
      const [title, visibility, url] = JSON.parse(await evaluate(send, c.context, 'JSON.stringify([document.title, document.visibilityState, location.href])'));
      infos.push({ context: c.context, title, visibility, url });
    } catch { /* a tab mid-navigation; skip it */ }
  }
  const pick = infos.find(i => want && i.title === want && i.visibility === 'visible')
    || infos.find(i => want && i.title === want)
    || (infos.filter(i => i.visibility === 'visible').length === 1 ? infos.find(i => i.visibility === 'visible') : null);
  if (!pick) throw new Error('Could not match this window to a Firefox tab');
  return pick;
}

// A PDF opens in Firefox's pdf.js viewer, whose DOM only holds the text of
// pages already rendered on screen, so agents fell back to screenshot+scroll.
// Pull every page's text layer from pdf.js instead; return
// '' while the document is still loading so the settle loop keeps waiting.
const PAGE_TEXT = `(async () => {
  const app = window.PDFViewerApplication;
  if (app) {
    const doc = app.pdfDocument;
    if (!doc) return '';
    const pages = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const content = await (await doc.getPage(i)).getTextContent();
      pages.push('--- page ' + i + ' of ' + doc.numPages + ' ---\\n' +
        content.items.map(item => item.str + (item.hasEOL ? '\\n' : ' ')).join(''));
    }
    return pages.join('\\n\\n');
  }
  return (document.body || document.documentElement).innerText;
})()`;

async function readViaBidi(windowTitle, tabUrl) {
  return bidi(async send => {
    let tab;
    if (tabUrl) {
      const { contexts } = await send('browsingContext.getTree', { maxDepth: 0 });
      // Firefox context IDs are scoped to each short-lived BiDi session.
      const context = contexts.find(c => c.url === tabUrl)?.context;
      if (!context) throw new Error('No browser tab matches that URL');
      const [title, url] = JSON.parse(await evaluate(send, context, 'JSON.stringify([document.title, location.href])'));
      tab = { context, title, url };
    } else tab = await contextForWindow(send, windowTitle);
    // Wait for client-rendered pages (SharePoint, Teams, My Account) to settle:
    // text must be non-empty and unchanged across three reads (~1.6s), up to
    // ~16s. Two reads were not enough -- a SPA's loading shell can hold still
    // that long (one page read 156 characters, then 1197).
    let text = '', previous = null, stable = 0;
    for (let i = 0; i < 20; i++) {
      text = String(await evaluate(send, tab.context, PAGE_TEXT) ?? '');
      if (text.trim() && text === previous) { if (++stable >= 2) break; } else stable = 0;
      previous = text;
      await sleep(800);
    }
    const [title, url] = JSON.parse(await evaluate(send, tab.context, 'JSON.stringify([document.title, location.href])'));
    return { ...tab, title, url, text };
  });
}

// SharePoint/OneDrive file links (PDF, Word, PowerPoint, Excel) download
// instead of opening, and SharePoint's own viewer renders pages as images --
// so agents fell back to screenshot+scroll. browser_file_text
// downloads the file through the signed-in browser and extracts its text:
// PDFs with poppler's pdftotext, Office Open XML by unzipping it.
const DOWNLOADS = process.env.MALRUNAR_MSTEAMS_MCP_DOWNLOADS_DIR || join(homedir(), 'Downloads');

function downloadsSnapshot() {
  try {
    return new Map(readdirSync(DOWNLOADS).map(f => {
      const st = statSync(join(DOWNLOADS, f));
      return [f, { mtime: st.mtimeMs, size: st.size }];
    }));
  } catch { return new Map(); }
}

// Firefox writes "<name>.part" beside a placeholder "<name>" and renames when
// done, so a download is complete once no .part remains and the file has bytes.
async function waitForNewDownload(before, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const now = downloadsSnapshot();
    const partial = [...now.keys()].some(f => f.endsWith('.part'));
    const fresh = [...now].filter(([f, v]) => !f.endsWith('.part') && v.size > 0
      && (!before.has(f) || before.get(f).mtime !== v.mtime));
    if (fresh.length && !partial) {
      fresh.sort((a, b) => b[1].mtime - a[1].mtime);
      return join(DOWNLOADS, fresh[0][0]);
    }
    await sleep(500);
  }
  return null;
}

const OOXML_TEXT_PY = String.raw`
import html, re, sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); names = z.namelist()
def text(xml):
    xml = re.sub(r'</(w:p|a:p|w:tr|row|si)>', '\n', xml)
    xml = re.sub(r'<(w:tab|w:br|a:br)\b[^>]*/>', ' ', xml)
    return html.unescape(re.sub(r'<[^>]+>', '', xml))
out = []
if 'word/document.xml' in names:
    out.append(text(z.read('word/document.xml').decode('utf-8')))
slides = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=lambda n: int(re.findall(r'\d+', n)[-1]))
for i, n in enumerate(slides, 1):
    out.append('--- slide %d of %d ---\n' % (i, len(slides)) + text(z.read(n).decode('utf-8')))
if 'xl/sharedStrings.xml' in names:
    out.append('(Excel: cell text only, without the grid)\n' + text(z.read('xl/sharedStrings.xml').decode('utf-8')))
print('\n'.join(out))
`;

async function fileText(windowTitle, url) {
  const before = downloadsSnapshot();
  await bidi(async send => {
    const tab = await contextForWindow(send, windowTitle);
    // A download aborts the navigation; that is the expected outcome here.
    const navigation = send('browsingContext.navigate', { context: tab.context, url, wait: 'complete' }, 60000);
    if (EXTENSION_BROWSER) await navigation;
    else await navigation.catch(() => {});
  });
  const file = await waitForNewDownload(before, 90000);
  if (!file) throw new Error('No file was downloaded from that URL. If it opens as a web page, use browser_navigate + browser_read instead.');
  const name = file.split('/').pop();
  const header = `File: ${name} (${statSync(file).size} bytes), saved to ${file}`;
  const ext = (name.match(/\.([a-z0-9]+)$/i)?.[1] || '').toLowerCase();
  let text;
  if (ext === 'pdf') {
    // Not pdf.js: a PDF may be downloaded instead of rendered (pdfjs.disabled), so
    // poppler's pdftotext extracts it; form feeds separate the pages.
    const r = spawnSync('pdftotext', ['-layout', '-enc', 'UTF-8', file, '-'], { encoding: 'utf8', timeout: 120000, maxBuffer: 64 * 1024 * 1024 });
    if (r.status !== 0) throw new Error(`${header}\nCould not extract PDF text: ${(r.stderr || r.error?.message || '').trim().slice(0, 300)}`);
    const pages = r.stdout.split('\f').filter((page, i, all) => page.trim() || i < all.length - 1);
    text = pages.map((page, i) => `--- page ${i + 1} of ${pages.length} ---\n${page}`).join('\n\n');
  } else if (['docx', 'pptx', 'xlsx', 'docm', 'pptm', 'xlsm'].includes(ext)) {
    const r = spawnSync('python3', ['-c', OOXML_TEXT_PY, file], { encoding: 'utf8', timeout: 60000, maxBuffer: 64 * 1024 * 1024 });
    if (r.status !== 0) throw new Error(`${header}\nCould not extract text: ${(r.stderr || '').trim().slice(0, 300)}`);
    text = r.stdout;
  } else if (['txt', 'csv', 'md', 'json', 'xml', 'html', 'htm'].includes(ext)) {
    text = readFileSync(file, 'utf8');
  } else {
    return `${header}\n(No text extractor for .${ext || '?'} files -- only PDF, Word/PowerPoint/Excel .docx/.pptx/.xlsx and plain text.)`;
  }
  return `${header}\n\n${tidy(text) || '(The file has no extractable text -- it may be a scanned image.)'}`;
}

async function navigateViaBidi(windowTitle, url) {
  return bidi(async send => {
    const tab = await contextForWindow(send, windowTitle);
    // A URL that downloads a file aborts the navigation; that is not a BiDi
    // failure, so don't fall back to xdotool and fetch it a second time.
    const navigation = send('browsingContext.navigate', { context: tab.context, url, wait: 'complete' }, 60000);
    if (EXTENSION_BROWSER) await navigation;
    else await navigation.catch(() => {});
    return tab;
  });
}
function integer(value, name, min = 0, max = 10000) {
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${name} must be an integer from ${min} to ${max}`);
  return String(value);
}

const tools = [
  { name: 'browser_windows', description: 'List Firefox work-browser windows on all virtual desktops and their IDs.', inputSchema: { type: 'object', properties: {} } },
  { name: 'browser_tabs', description: 'List URLs and titles of tabs in the shared work browser, including background tabs. Use a URL as browser_read tab_url.', inputSchema: { type: 'object', properties: {} } },
  { name: 'browser_screenshot', description: 'View a Firefox work-browser window. Coordinates in the image can be used with browser_click.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' } } } },
  { name: 'browser_read', description: 'Read the current page, or a background tab using tab_url from browser_tabs, without changing focus. Returns URL, title and DOM text and waits for client-rendered pages to settle. Use screenshots only for canvas/report visuals.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, tab_url: { type: 'string' } } } },
  { name: 'browser_file_text', description: 'Read a document file (PDF, Word .docx, PowerPoint .pptx, Excel .xlsx, or plain text) from a URL -- typically a SharePoint/OneDrive document link, which downloads instead of opening. Downloads it through the signed-in browser and returns the extracted text. Use this instead of opening documents in a viewer and screenshotting pages.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, url: { type: 'string' } }, required: ['url'] } },
  { name: 'browser_click', description: 'Click at screenshot coordinates in a Firefox window.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, x: { type: 'integer' }, y: { type: 'integer' }, button: { type: 'integer', enum: [1, 2, 3] } }, required: ['x', 'y'] } },
  { name: 'browser_type', description: 'Type text into the focused field in a Firefox window.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, text: { type: 'string' } }, required: ['text'] } },
  { name: 'browser_key', description: 'Press an X11 key combination, such as Return, Tab, Escape, ctrl+l, or ctrl+a.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, key: { type: 'string' } }, required: ['key'] } },
  { name: 'browser_scroll', description: 'Scroll a Firefox window by wheel steps; positive values scroll down.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, steps: { type: 'integer' } }, required: ['steps'] } },
  { name: 'browser_navigate', description: 'Open an HTTP or HTTPS URL in the selected Firefox window and wait for it to finish loading.', inputSchema: { type: 'object', properties: { window_id: { type: 'string' }, url: { type: 'string' } }, required: ['url'] } },
];

const tidy = text => {
  text = text.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  return text.length > 60000 ? text.slice(0, 60000) + '\n…[truncated at 60000 chars]' : text;
};

async function call(name, args) {
  if (name === 'browser_windows') return [{ type: 'text', text: JSON.stringify(windows()) }];
  if (name === 'browser_tabs') return bidi(async send => {
    const { contexts } = await send('browsingContext.getTree', { maxDepth: 0 });
    const tabs = [];
    for (const c of contexts) tabs.push({ url: c.url,
      title: EXTENSION_BROWSER ? c.title : await evaluate(send, c.context, 'document.title') });
    return [{ type: 'text', text: JSON.stringify(tabs) }];
  });
  const { id, title } = windowFor(args);
  if (name === 'browser_read') {
    try {
      const page = await readViaBidi(title, args.tab_url);
      const text = tidy(page.text);
      return [{ type: 'text', text: `URL: ${page.url}\nTitle: ${page.title}\n\n${text || '(No text on this page -- it may be a canvas/report surface such as a Power BI visual. Use browser_screenshot.)'}` }];
    } catch (error) {
      if (EXTENSION_BROWSER || args.tab_url) throw error; // Honor the extension's site permissions.
      process.stderr.write(`browser_read: BiDi unavailable (${error.message}); using the clipboard fallback\n`);
    }
  }
  if (name === 'browser_file_text') {
    if (typeof args.url !== 'string' || !/^https?:\/\//i.test(args.url) || args.url.length > 4096) throw new Error('url must be an HTTP(S) URL');
    return [{ type: 'text', text: await fileText(title, args.url) }];
  }
  if (name === 'browser_navigate') {
    if (typeof args.url !== 'string' || !/^https?:\/\//i.test(args.url) || args.url.length > 2048) throw new Error('url must be an HTTP(S) URL');
    try {
      const before = downloadsSnapshot();
      await navigateViaBidi(title, args.url);
      const file = await waitForNewDownload(before, 2500);
      if (file) return [{ type: 'text', text: `That URL downloaded a file (${file.split('/').pop()}) instead of opening a page. Use browser_file_text with the same URL to read its text.` }];
      return [{ type: 'text', text: 'Loaded. Use browser_read to get the page text.' }];
    } catch (error) {
      if (EXTENSION_BROWSER) throw error;
      process.stderr.write(`browser_navigate: BiDi unavailable (${error.message}); using xdotool\n`);
    }
  }
  if (EXTENSION_BROWSER) {
    // X11 remains useful for full-window screenshots and real keyboard/mouse
    // input, but it must not bypass a site's denied or missing page permission.
    await bidi(async send => {
      const tab = await contextForWindow(send, title);
      await evaluate(send, tab.context, 'true');
    });
  }
  if (name === 'browser_screenshot') {
    const dir = mkdtempSync(join(tmpdir(), 'work-browser-'));
    try {
      const png = join(dir, 'window.png');
      run('import', ['-silent', '-window', id, '-strip', png]);
      return [{ type: 'image', data: readFileSync(png).toString('base64'), mimeType: 'image/png' }];
    } finally { rmSync(dir, { recursive: true, force: true }); }
  }
  focus(id);
  if (name === 'browser_read') {
    // No DOM access here (this drives Firefox over X11, not Playwright), so read
    // the rendered text via the clipboard: Escape any open menu, Select-All,
    // Copy, then read the clipboard. Firefox writes the clipboard
    // asynchronously, so poll xclip briefly.
    run('xdotool', ['key', '--clearmodifiers', 'Escape']);
    run('xdotool', ['key', '--clearmodifiers', 'ctrl+a']);
    run('xdotool', ['key', '--clearmodifiers', 'ctrl+c']);
    let text = '';
    for (let i = 0; i < 8; i++) {
      const r = spawnSync('xclip', ['-selection', 'clipboard', '-o'], { encoding: 'utf8', timeout: 5000, maxBuffer: 16 * 1024 * 1024 });
      if (r.status === 0 && r.stdout.trim()) { text = r.stdout; break; }
      spawnSync('sleep', ['0.3']);
    }
    text = text.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    if (!text) return [{ type: 'text', text: '(No selectable text on this page — it may be a canvas/report surface such as a Power BI visual. Use browser_screenshot instead.)' }];
    if (text.length > 60000) text = text.slice(0, 60000) + '\n…[truncated at 60000 chars]';
    return [{ type: 'text', text }];
  }
  if (name === 'browser_click') {
    run('xdotool', ['mousemove', '--window', id, integer(args.x, 'x'), integer(args.y, 'y'), 'click', integer(args.button ?? 1, 'button', 1, 3)]);
  } else if (name === 'browser_type') {
    if (typeof args.text !== 'string' || args.text.length > 10000) throw new Error('text must be a string of at most 10000 characters');
    run('xdotool', ['type', '--clearmodifiers', '--delay', '1', '--', args.text]);
  } else if (name === 'browser_key') {
    if (typeof args.key !== 'string' || !/^[a-zA-Z0-9_+\-]{1,40}$/.test(args.key)) throw new Error('Invalid X11 key combination');
    run('xdotool', ['key', '--clearmodifiers', args.key]);
  } else if (name === 'browser_scroll') {
    const count = Number(integer(Math.abs(args.steps), 'steps', 0, 30));
    for (let i = 0; i < count; i++) run('xdotool', ['click', args.steps >= 0 ? '5' : '4']);
  } else if (name === 'browser_navigate') {
    if (typeof args.url !== 'string' || !/^https?:\/\//i.test(args.url) || args.url.length > 2048) throw new Error('url must be an HTTP(S) URL');
    run('xdotool', ['key', '--clearmodifiers', 'ctrl+l']);
    run('xdotool', ['type', '--clearmodifiers', '--delay', '1', '--', args.url]);
    run('xdotool', ['key', 'Return']);
  } else throw new Error(`Unknown tool: ${name}`);
  return [{ type: 'text', text: 'Done. Use browser_read for the page text (browser_screenshot only for visuals).' }];
}

const send = value => process.stdout.write(JSON.stringify(value) + '\n');
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let request;
  try {
    request = JSON.parse(line);
    if (request.method === 'notifications/initialized' || request.id === undefined) continue;
    let result;
    if (request.method === 'initialize') result = { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'malrunar-work-browser-mcp', version: '1.0.0' } };
    else if (request.method === 'tools/list') result = { tools };
    else if (request.method === 'tools/call') {
      try { result = { content: await call(request.params?.name, request.params?.arguments || {}) }; }
      catch (error) { result = { content: [{ type: 'text', text: error.message }], isError: true }; }
    } else throw new Error(`Unknown method: ${request.method}`);
    send({ jsonrpc: '2.0', id: request.id, result });
  } catch (error) {
    if (request?.id !== undefined) send({ jsonrpc: '2.0', id: request.id, error: { code: -32603, message: error.message } });
  }
}
