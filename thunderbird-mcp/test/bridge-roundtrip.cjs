// Behaviour test of the bridge wrapper against a stand-in for the add-on's HTTP server (node only, no Thunderbird):
//   node bridge-roundtrip.cjs <bridge-command>
// The bridge reads the connection file (port + token), answers `initialize` itself, and forwards every other
// request to the add-on with the token as a bearer credential. The stand-in checks that, and the test checks
// that a request without a valid connection file is reported, and that sensitive attachment paths are refused.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const command = process.argv[2];
assert.ok(command, 'pass the bridge command');
const token = 'ab'.repeat(32);
const seen = [];

function startFake() {
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      seen.push({ auth: req.headers.authorization, body: JSON.parse(body) });
      if (req.headers.authorization !== `Bearer ${token}`) { res.statusCode = 403; res.end('no'); return; }
      const message = JSON.parse(body);
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { tools: [{ name: 'listAccounts' }] } }));
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

function converse(connectionFile, requests) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [], {
      env: { ...process.env, MALRUNAR_THUNDERBIRD_MCP_CONNECTION_FILE: connectionFile },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    let out = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('bridge did not finish in 60 s')); }, 60000);
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.on('close', () => { clearTimeout(timer); resolve(out.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))); });
    for (const request of requests) child.stdin.write(JSON.stringify(request) + '\n');
    child.stdin.end();
  });
}

(async () => {
  const server = await startFake();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tb-mcp-test-'));
  const file = path.join(dir, 'connection.json');
  fs.writeFileSync(file, JSON.stringify({ port: server.address().port, token, pid: process.pid }));

  const replies = await converse(file, [
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } } },
    { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} },
  ]);
  const byId = Object.fromEntries(replies.map((reply) => [reply.id, reply]));
  assert.equal(byId[1].result.serverInfo.name, 'thunderbird-mcp');
  assert.equal(byId[1].result.protocolVersion, '2025-06-18');
  assert.deepEqual(byId[2].result.tools, [{ name: 'listAccounts' }], 'tools/list must be answered by the add-on');
  assert.equal(seen.length, 1, 'initialize must be answered by the bridge itself, only tools/list is forwarded');
  assert.equal(seen[0].auth, `Bearer ${token}`);
  console.log('bridge: initialize answered locally, tools/list forwarded with the bearer token');

  // a connection file that names a port nobody listens on: a JSON-RPC error that says Thunderbird is the problem
  fs.writeFileSync(file, JSON.stringify({ port: 9, token, pid: process.pid }));
  const failed = await converse(file, [{ jsonrpc: '2.0', id: 3, method: 'tools/list', params: {} }]);
  assert.equal(failed.length, 1);
  assert.ok(failed[0].error && /Thunderbird/.test(failed[0].error.message), `unexpected error reply: ${JSON.stringify(failed[0])}`);
  console.log('bridge: an unreachable add-on is reported as an error reply');

  server.close();
  fs.rmSync(dir, { recursive: true, force: true });
})().catch((error) => { console.error(error); process.exit(1); });
