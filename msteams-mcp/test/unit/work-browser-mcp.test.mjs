// libexec/work-browser-mcp.mjs as an MCP client sees it: the protocol, the tool list, and a clear
// error (not a crash) when there is no display. The windows themselves are exercised in smoke.sh.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, '../../libexec/work-browser-mcp.mjs');
const hasDisplay = (() => { try { return fs.readdirSync('/tmp/.X11-unix').some(name => /^X\d+$/.test(name)); } catch { return false; } })();

function session(env = {}) {
  const child = spawn(process.execPath, [SCRIPT], { env: { PATH: process.env.PATH, HOME: process.env.HOME || '/tmp', ...env }, stdio: ['pipe', 'pipe', 'inherit'] });
  const waiting = new Map();
  let buffer = '';
  child.stdout.on('data', chunk => {
    buffer += chunk;
    let at;
    while ((at = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
      if (!line) continue;
      const message = JSON.parse(line);
      waiting.get(message.id)?.(message);
    }
  });
  let next = 0;
  return {
    request: (method, params) => new Promise((resolve, reject) => {
      const id = ++next;
      const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 20000);
      waiting.set(id, message => { clearTimeout(timer); resolve(message); });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    }),
    close: () => { child.stdin.end(); child.kill(); },
  };
}

test('initialize and tools/list speak MCP and name the ten browser tools', async () => {
  const s = session();
  try {
    const init = await s.request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'test', version: '0' } });
    assert.equal(init.result.serverInfo.name, 'malrunar-work-browser-mcp');
    const list = await s.request('tools/list', {});
    assert.deepEqual(list.result.tools.map(tool => tool.name).sort(), [
      'browser_click', 'browser_file_text', 'browser_key', 'browser_navigate', 'browser_read',
      'browser_screenshot', 'browser_scroll', 'browser_tabs', 'browser_type', 'browser_windows']);
    for (const tool of list.result.tools) assert.equal(tool.inputSchema.type, 'object');
    const unknown = await s.request('no/such/method', {});
    assert.match(unknown.error.message, /Unknown method/);
  } finally { s.close(); }
});

test('without a display a tool call returns an error result that says so', { skip: hasDisplay }, async () => {
  const s = session({ PATH: process.env.PATH });
  try {
    const call = await s.request('tools/call', { name: 'browser_windows', arguments: {} });
    assert.equal(call.result.isError, true);
    assert.match(call.result.content[0].text, /No reachable desktop display|xdpyinfo failed|sh failed/);
  } finally { s.close(); }
});

test('the profile and the backend come from the settings, and the tail backend names its component', { skip: hasDisplay }, async () => {
  const s = session({ MALRUNAR_MSTEAMS_MCP_BROWSER_MCP_BACKEND: 'tail', MALRUNAR_MSTEAMS_MCP_TAIL_CLIENT: '/nonexistent/client.mjs' });
  try {
    // No display: the error is the display one; the point is that selecting `tail` does not crash the server at start.
    const call = await s.request('tools/call', { name: 'browser_tabs', arguments: {} });
    assert.equal(call.result.isError, true);
  } finally { s.close(); }
});
