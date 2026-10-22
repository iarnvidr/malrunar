import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { pathToFileURL } from 'node:url';

const root = process.env.TAIL_TEST_ROOT || '/opt/malrunar/tail-browser-mcp';
const browserId = process.env.TAIL_BROWSER_ID || 'browser';
const { connection } = await import(pathToFileURL(`${root}/client.mjs`));
let server, ws;
const tabs = [
  { id: 7, url: 'https://example.com/?a=,b', title: 'A, B\nC', active: true, status: 'complete' },
  { id: 8, url: 'https://denied.example/', title: 'Denied', active: false, status: 'complete' },
  { id: 10, url: 'https://example.com/background', title: 'Sleeping', active: false, status: 'complete', discarded: true },
];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const listener = createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  return port;
}

before(async () => {
  const httpPort = await freePort(), wsPort = await freePort();
  process.env.TAIL_MCP_URL = `http://127.0.0.1:${httpPort}/mcp`;
  server = spawn(process.execPath, [`${root}/server.cjs`], {
    env: { ...process.env, CONTAINERIZED: '', MCP_HTTP_PORT: String(httpPort), EXTENSION_PORT: String(wsPort) },
    stdio: 'ignore',
  });
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { ready = (await fetch(`http://127.0.0.1:${httpPort}/health`)).ok; } catch {}
    if (ready) break;
    await delay(50);
  }
  assert.ok(ready, 'Tail server started');
  ws = new WebSocket(`ws://127.0.0.1:${wsPort}`);
  ws.onmessage = event => {
    const request = JSON.parse(event.data).payload;
    if (request.type === 'register-ack') return;
    const { correlationId } = request;
    if (request.cmd === 'get-tab-list') {
      ws.send(JSON.stringify({ payload: { correlationId, resource: 'tabs', tabs } }));
    } else if (request.cmd === 'open-tab') {
      const tab = tabs.find(tab => tab.id === request.tabId);
      assert.ok(tab);
      tab.url = request.url;
      tab.discarded = false;
      ws.send(JSON.stringify({ payload: { correlationId, resource: 'opened-tab-id', tabId: tab.id } }));
    } else if (request.cmd === 'evaluate-script') {
      if (request.tabId === 8) {
        ws.send(JSON.stringify({ correlationId, errorMessage: 'Domain in tab URL is in the deny list' }));
      } else {
        let result = 'Page text';
        if (request.function.includes('location.assign')) { tabs[0].url = request.args[0]; result = true; }
        ws.send(JSON.stringify({ payload: { correlationId, resource: 'evaluate-script-result', tabId: request.tabId, result } }));
      }
    }
  };
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.send(JSON.stringify({ payload: { type: 'register', browserId, browserType: 'firefox' } }));
  for (let i = 0; i < 100; i++) {
    const health = await (await fetch(`http://127.0.0.1:${httpPort}/health`)).json();
    if (health.browsers.length) return;
    await delay(50);
  }
  throw new Error('Test extension did not register');
});

after(async () => {
  ws?.close();
  if (server && server.exitCode === null) {
    server.kill('SIGTERM');
    await new Promise(resolve => server.once('exit', resolve));
  }
});

test('actual HTTP MCP round trip preserves tab metadata and page reads', async () => {
  await connection(async send => {
    const { contexts } = await send('browsingContext.getTree');
    assert.equal(contexts[0].url, tabs[0].url);
    assert.equal(contexts[0].title, 'A, B\nC');
    assert.equal(contexts[0].active, true);
    const read = await send('script.evaluate', { target: { context: '7' }, expression: 'document.body.innerText' });
    assert.equal(read.result.value, 'Page text');
  });
});

test('site denial propagates as an error instead of falling back to another transport', async () => {
  await assert.rejects(connection(send => send('script.evaluate', {
    target: { context: '8' }, expression: 'document.body.innerText',
  })), /deny list/);
});

test('navigation uses the same tab and waits for its load status', async () => {
  await connection(send => send('browsingContext.navigate', { context: '7', url: 'https://example.com/next' }));
  assert.equal(tabs[0].url, 'https://example.com/next');
});

test('reading a sleeping background tab loads it without activating it', async () => {
  await connection(async send => {
    const read = await send('script.evaluate', { target: { context: '10' }, expression: 'document.body.innerText' });
    assert.equal(read.result.value, 'Page text');
  });
  assert.equal(tabs[2].discarded, false);
  assert.equal(tabs[2].active, false);
  assert.equal(tabs[0].active, true);
});
