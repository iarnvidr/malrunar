// An MCP client for `malrunar-msteams-mcp browser-mcp`: lists the windows and tabs of the visible
// browser, reads a page through WebDriver BiDi, takes a screenshot and navigates.
//   browser-mcp-client.mjs <fixture url> <second url> <expected text of the first> <expected text of the second>
import { spawn } from 'node:child_process';

const [first, second, expectFirst, expectSecond] = process.argv.slice(2);
const child = spawn('/opt/malrunar/msteams-mcp/bin/malrunar-msteams-mcp', ['browser-mcp'], { stdio: ['pipe', 'pipe', 'inherit'] });
const waiting = new Map();
let buffer = '';
child.stdout.on('data', chunk => {
  buffer += chunk;
  let at;
  while ((at = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
    if (line) { const message = JSON.parse(line); waiting.get(message.id)?.(message); }
  }
});
let next = 0;
const request = (method, params) => new Promise((resolve, reject) => {
  const id = ++next;
  const timer = setTimeout(() => reject(new Error(`${method} ${JSON.stringify(params)} timed out`)), 90000);
  waiting.set(id, message => { clearTimeout(timer); resolve(message); });
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
});
const call = async (name, args = {}) => {
  const message = await request('tools/call', { name, arguments: args });
  if (message.error) throw new Error(`${name}: ${message.error.message}`);
  if (message.result.isError) throw new Error(`${name}: ${message.result.content[0].text}`);
  return message.result.content;
};
const assert = (condition, text) => { if (!condition) { console.error('FAILED: ' + text); child.kill(); process.exit(1); } };

await request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'smoke', version: '0' } });

const windows = JSON.parse((await call('browser_windows'))[0].text);
console.log('windows', JSON.stringify(windows));
assert(windows.length >= 1, 'no visible browser window of the work browser profile');

const tabs = JSON.parse((await call('browser_tabs'))[0].text);
console.log('tabs', JSON.stringify(tabs));
assert(tabs.some(tab => tab.url === first), `the fixture tab is not listed: ${first}`);

const read = (await call('browser_read'))[0].text;
console.log('read:', read.replace(/\n+/g, ' | ').slice(0, 200));
assert(read.includes(expectFirst), `browser_read did not return the page text '${expectFirst}'`);
assert(read.includes(`URL: ${first}`), 'browser_read did not report the URL');

const shot = (await call('browser_screenshot'))[0];
assert(shot.type === 'image' && shot.mimeType === 'image/png' && Buffer.from(shot.data, 'base64').subarray(1, 4).toString() === 'PNG', 'browser_screenshot is not a PNG');

const navigated = (await call('browser_navigate', { url: second }))[0].text;
console.log('navigate:', navigated);
const after = (await call('browser_read'))[0].text;
assert(after.includes(expectSecond), `after browser_navigate the page text is not '${expectSecond}': ${after.slice(0, 200)}`);

let refused = false;
try { await call('browser_navigate', { url: 'file:///etc/passwd' }); } catch (error) { refused = /HTTP\(S\)/.test(error.message); }
assert(refused, 'browser_navigate accepted a non-HTTP URL');

child.stdin.end();
child.kill();
console.log('browser-mcp client ok');
process.exit(0);
