// The bridge's own safeguards, as upstream ships them: node bridge-unit.cjs <path to mcp-bridge.cjs>
'use strict';
const assert = require('node:assert/strict');
const { isSensitiveFilePath, isValidAuthToken } = require(require('node:path').resolve(process.argv[2]));
for (const sensitive of ['/home/user/.ssh/id_rsa', '/home/user/.aws/credentials', '/etc/shadow', '/home/user/.gnupg/secring.gpg']) {
  assert.equal(isSensitiveFilePath(sensitive), true, `${sensitive} must be refused as an attachment`);
}
for (const fine of ['/tmp/report.pdf', '/srv/data/photo.png']) {
  assert.equal(isSensitiveFilePath(fine), false, `${fine} must be allowed`);
}
assert.equal(isValidAuthToken('ab'.repeat(32)), true);
assert.equal(isValidAuthToken('AB'.repeat(32)), false);
assert.equal(isValidAuthToken('short'), false);
console.log('bridge: sensitive attachment paths refused, token format checked');
