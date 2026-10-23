// The bridge against the real add-on inside a running Thunderbird: node bridge-e2e.cjs <bridge-command>
// The add-on writes its connection file (port and token) when its server is up; the bridge finds it by itself.
'use strict';
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');

const command = process.argv[2];
assert.ok(command, 'pass the bridge command');

function converse(requests) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [], { stdio: ['pipe', 'pipe', 'inherit'] });
    let out = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('no answer in 90 s')); }, 90000);
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.on('close', () => { clearTimeout(timer); resolve(out.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))); });
    for (const request of requests) child.stdin.write(JSON.stringify(request) + '\n');
    child.stdin.end();
  });
}

(async () => {
  const replies = await converse([
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } } },
    { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} },
    { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'listAccounts', arguments: {} } },
  ]);
  const byId = Object.fromEntries(replies.map((reply) => [reply.id, reply]));
  assert.equal(byId[1].result.serverInfo.name, 'thunderbird-mcp');
  const names = (byId[2].result?.tools || []).map((tool) => tool.name);
  assert.ok(names.includes('listAccounts') && names.includes('listFolders') && names.includes('searchMessages'), `tools/list from the add-on: ${names.join(',')}`);
  assert.ok(byId[3].result && !byId[3].error, `listAccounts: ${JSON.stringify(byId[3])}`);
  console.log(`bridge + add-on in Thunderbird: ${names.length} tools, listAccounts answered`);
})().catch((error) => { console.error(error); process.exit(1); });
