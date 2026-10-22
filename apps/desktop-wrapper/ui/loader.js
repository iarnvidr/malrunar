// Checks that the site answers, then hands the window over to it. A no-cors fetch cannot read the
// response (it is opaque) but it only rejects when the network request itself fails, which is all we
// need to know. Only an http(s) target is ever followed.
const params = new URLSearchParams(location.search);
const name = params.get('name') || 'the site';
let target = null;
try {
  const url = new URL(params.get('target'));
  if (url.protocol === 'https:' || url.protocol === 'http:') target = url.href;
} catch { /* no usable target */ }

document.title = name;
for (const element of document.querySelectorAll('.name')) element.textContent = name;
document.getElementById('host').textContent = target ? new URL(target).host : '(no address)';

const connecting = document.getElementById('connecting');
const offline = document.getElementById('offline');
const RETRY_MS = 15000;
let timer;

async function connect(manual = false) {
  clearTimeout(timer);
  if (!target) {
    connecting.hidden = true;
    offline.hidden = false;
    return;
  }
  if (manual) {
    connecting.hidden = false;
    offline.hidden = true;
  }
  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), 8000);
  try {
    await fetch(target, { mode: 'no-cors', cache: 'no-store', signal: controller.signal });
    location.replace(target);
  } catch {
    connecting.hidden = true;
    offline.hidden = false;
    timer = setTimeout(connect, RETRY_MS);
  } finally {
    clearTimeout(abort);
  }
}

document.getElementById('retry').addEventListener('click', () => connect(true));
connect();
