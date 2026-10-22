# SPDX-License-Identifier: Apache-2.0
"""A small built-in control page at GET /. Everything it does is available through the API."""
from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
__REDIRECT__
<style>
:root { color-scheme: dark; --bg:#0b0d12; --panel:#141821; --border:#2a3242; --text:#f2f5fa; --muted:#9aa6b8; --blue:#3b82f6; --red:#e5484d; --green:#48c78e; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui,sans-serif; }
main { width:min(1000px,100%); margin:0 auto; padding:24px 16px 48px; }
h1 { margin:0 0 4px; font-size:1.6rem; } h2 { margin:0 0 10px; font-size:1.05rem; }
.muted { color:var(--muted); }
section { margin-top:18px; padding:16px; border:1px solid var(--border); border-radius:14px; background:var(--panel); }
.status { display:inline-block; padding:4px 12px; border:1px solid var(--border); border-radius:999px; }
.status.ok { border-color:var(--green); } .status.bad { border-color:var(--red); }
button, input { font:inherit; color:var(--text); background:#1b2130; border:1px solid var(--border); border-radius:10px; padding:9px 14px; }
button { cursor:pointer; } button:hover { border-color:#506079; } button:disabled { opacity:.5; cursor:wait; }
button[aria-pressed="true"] { border-color:var(--blue); background:rgba(59,130,246,.2); }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
img.preview { width:100%; max-width:640px; aspect-ratio:16/9; background:#000; border-radius:10px; }
iframe.sender { width:100%; height:520px; border:0; border-radius:10px; background:#000; }
.clip { display:flex; justify-content:space-between; gap:10px; padding:8px 0; border-top:1px solid var(--border); }
#result.error { color:#ff8f93; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="row"><span class="status" id="status">Checking output...</span>
  <button onclick="act('api/start-output')">Start output</button>
  <button onclick="act('api/stop-output')">Stop output</button>
  <button onclick="act('api/restart-output')">Restart output</button>
  <button onclick="act('api/restart-live')">Restart camera</button></p>
  __SENDER__
  <section><h2>Program</h2>
    <img class="preview" id="preview" alt="Program preview">
    <p class="row" id="scenes"></p>
  </section>
  <section><h2>Recording</h2>
    <div class="row"><input id="name" maxlength="60" placeholder="Clip name" aria-label="Clip name">
    <button id="rec-start" onclick="startRecording()">Start recording</button>
    <button id="rec-stop" onclick="stopRecording()" disabled>Stop and prepare clip</button></div>
    <p class="muted" id="rec-state" role="status">Recorder ready</p>
  </section>
  <section><h2>Clips</h2><div id="clips"></div><p class="muted" id="no-clips">No recorded clips yet.</p></section>
  <p id="result" role="status" aria-live="polite" class="muted"></p>
</main>
<script>
const PROGRAM_CONTROL = __PROGRAM_CONTROL__;
const $ = s => document.querySelector(s);
async function act(path, options) {
  $('#result').className = 'muted'; $('#result').textContent = 'Working...';
  try {
    const r = await fetch(path, Object.assign({method: 'POST'}, options || {}));
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    $('#result').textContent = 'Done'; await refresh();
  } catch (e) { $('#result').className = 'error'; $('#result').textContent = e.message; }
}
async function refresh() {
  try {
    const health = await (await fetch('health', {cache: 'no-store'})).json();
    $('#status').className = 'status ' + (health.healthy ? 'ok' : 'bad');
    $('#status').textContent = health.healthy ? `On air: ${health.scene}` : 'Output not running';
    const data = await (await fetch('api/scenes', {cache: 'no-store'})).json();
    const box = $('#scenes'); box.replaceChildren();
    for (const [key, scene] of Object.entries(data.keys)) {
      const b = document.createElement('button');
      b.textContent = scene; b.setAttribute('aria-pressed', String(scene === data.program));
      b.onclick = () => act(`api/scene/${encodeURIComponent(key)}`); box.append(b);
    }
  } catch (e) { $('#status').className = 'status bad'; $('#status').textContent = 'Controller unavailable'; }
  $('#preview').src = 'api/program-preview.jpg?t=' + Date.now();
  try {
    const rec = await (await fetch('api/recording/status', {cache: 'no-store'})).json();
    $('#rec-start').disabled = !rec.available || rec.recording;
    $('#rec-stop').disabled = !rec.available || !rec.recording;
    $('#rec-state').textContent = !rec.available ? 'Recorder unavailable' : rec.recording ? `Recording ${Math.round(rec.duration || 0)} s` : (rec.finalizing ? 'Preparing the MP4...' : 'Recorder ready');
  } catch (e) { $('#rec-state').textContent = 'Recorder unavailable'; }
}
async function loadClips() {
  const data = await (await fetch('api/clips', {cache: 'no-store'})).json();
  const box = $('#clips'); box.replaceChildren(); $('#no-clips').hidden = data.clips.length > 0;
  for (const clip of data.clips) {
    const row = document.createElement('div'); row.className = 'clip';
    const name = document.createElement('span'); name.textContent = clip.display_name;
    const send = document.createElement('button'); send.textContent = 'Send to program';
    send.onclick = () => act(PROGRAM_CONTROL ? `api/clip/${encodeURIComponent(clip.id)}/play` : `api/clip/${encodeURIComponent(clip.id)}/scene`);
    row.append(name, send); box.append(row);
  }
}
async function startRecording() {
  await act('api/recording/start', {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: $('#name').value})});
}
async function stopRecording() { await act('api/recording/stop'); await loadClips(); }
refresh(); loadClips(); setInterval(() => { if (!document.hidden) refresh(); }, 2000);
</script>
</body>
</html>
"""


def render(ctl: "Controller") -> str:
    console = ctl.config.console
    sender = ""
    if console.sender_url:
        sender = (
            '<section><h2>Sender</h2><iframe class="sender" title="Sender" '
            f'src="{html.escape(console.sender_url, quote=True)}" '
            'allow="camera *; microphone *; display-capture *; autoplay; fullscreen"></iframe></section>'
        )
    redirect = ""
    if console.secure_url:
        redirect = f'<script>if(location.protocol!=="https:")location.replace({json.dumps(console.secure_url)});</script>'
    return (
        PAGE.replace("__TITLE__", html.escape(console.title))
        .replace("__REDIRECT__", redirect)
        .replace("__SENDER__", sender)
        .replace("__PROGRAM_CONTROL__", "true" if ctl.program_control else "false")
    )


def index(ctl: "Controller"):
    async def handler(_: web.Request) -> web.Response:
        return web.Response(content_type="text/html", text=render(ctl))

    return handler
