#!/usr/bin/env bash
# The synthetic fixture server (dev/fixture.mjs) serves made-up chats through the real server code.
set -euo pipefail
cd /tmp
node /opt/malrunar/teams-service/dev/fixture.mjs > /tmp/fixture.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
node - <<'NODE'
const base = 'http://127.0.0.1:28390', token = 'dev-token-dev-token-dev-token-dev-token-0000';
for (let i = 0; i < 60; i++) { try { if ((await fetch(base + '/health')).ok) break; } catch {} await new Promise(r => setTimeout(r, 500)); }
const list = await (await fetch(base + '/chats', { headers: { Authorization: 'Bearer ' + token } })).json();
if (!list.chats || list.chats.length < 20) { console.error('too few fixture chats', list.chats?.length); process.exit(1); }
const text = JSON.stringify(list);
for (const forbidden of [/@[a-z0-9-]+\.(dk|no|se)\b/i]) if (forbidden.test(text)) { console.error('fixture data looks real', forbidden); process.exit(1); }
if (!list.chats.some(c => c.title === 'Anna Example')) { console.error('the synthetic person is missing'); process.exit(1); }
console.log('fixture chats:', list.chats.length);
NODE
echo 'fixture test passed'
