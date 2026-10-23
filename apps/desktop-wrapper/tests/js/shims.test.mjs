// Runs the scripts injected into the page against small fakes of the browser objects they use.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const scripts = new URL('../../src-tauri/scripts/', import.meta.url);
const load = (name) => readFileSync(new URL(name, scripts), 'utf8');

class FakeElement {
  constructor(href, target = '') { this.href = href; this.target = target; }
  closest(selector) { return selector === 'a[href]' ? this : null; }
}

function linkEnvironment(hosts) {
  const opened = [];
  let listener;
  const document = { addEventListener: (type, fn, capture) => { assert.equal(type, 'click'); assert.equal(capture, true); listener = fn; } };
  const window = { open: (...args) => opened.push(args) };
  const location = { href: 'https://app.example.com/inbox' };
  new Function('cfg', 'document', 'window', 'location', 'Element', load('links.js'))({ hosts }, document, window, location, FakeElement);
  const click = (href, { target = '', button = 0, defaultPrevented = false } = {}) => {
    const event = { target: new FakeElement(href, target), button, defaultPrevented, prevented: false, preventDefault() { this.prevented = true; } };
    listener(event);
    return event;
  };
  return { opened, click };
}

test('links to the site stay, links elsewhere go through window.open', () => {
  const { opened, click } = linkEnvironment(['app.example.com', 'login.example.org', '*.idp.example.net']);
  assert.equal(click('/next').prevented, false);
  assert.equal(click('https://app.example.com/x').prevented, false);
  assert.equal(click('https://login.example.org/auth').prevented, false);
  assert.equal(click('https://tenant.idp.example.net/').prevented, false);
  assert.deepEqual(opened, []);

  for (const href of ['https://example.com/', 'https://app.example.com.evil.example/', 'https://idp.example.net/', 'http://app.example.com/y'.replace('http', 'ftp')]) {
    const event = click(href);
    assert.equal(event.prevented, href.startsWith('ftp') ? false : true, href);
  }
  assert.deepEqual(opened.map((args) => args[0]), [
    'https://example.com/', 'https://app.example.com.evil.example/', 'https://idp.example.net/',
  ]);
  assert.ok(opened.every((args) => args[1] === '_blank'));
});

test('target=_blank links always go through window.open; other clicks are left alone', () => {
  const { opened, click } = linkEnvironment(['app.example.com']);
  assert.equal(click('/popup', { target: '_blank' }).prevented, true);
  assert.deepEqual(opened, [['https://app.example.com/popup', '_blank']]);
  assert.equal(click('https://example.com/', { button: 1 }).prevented, false);
  assert.equal(click('https://example.com/', { defaultPrevented: true }).prevented, false);
  assert.equal(click('mailto:a@example.com').prevented, false);
});

test('a site on a non-default port matches host and port', () => {
  const { click } = linkEnvironment(['127.0.0.1:8080']);
  assert.equal(click('http://127.0.0.1:8080/a').prevented, false);
  assert.equal(click('http://127.0.0.1:9090/a').prevented, true);
});

function notificationEnvironment({ failing = false } = {}) {
  const calls = [];
  const window = {
    focused: 0,
    focus() { this.focused += 1; },
    __TAURI_INTERNALS__: { invoke: (command, args) => { calls.push([command, args]); return failing ? Promise.reject(new Error('no')) : Promise.resolve(); } },
  };
  const navigator = {};
  new Function('cfg', 'window', 'navigator', load('notifications.js'))({ hosts: [] }, window, navigator);
  return { window, navigator, calls };
}

test('new Notification hands title and body to the app and reports show', async () => {
  const { window, calls } = notificationEnvironment();
  const shown = [];
  const notification = new window.Notification('Hello', { body: 'World', tag: 't', data: { a: 1 } });
  notification.onshow = () => shown.push('onshow');
  notification.addEventListener('show', () => shown.push('listener'));
  assert.deepEqual(calls, [['wrapper_notify', { id: '1', title: 'Hello', body: 'World' }]]);
  assert.equal(notification.tag, 't');
  assert.deepEqual(notification.data, { a: 1 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(shown, ['onshow', 'listener']); // handlers set after construction still fire: show is asynchronous
});

test('permission is granted and requestPermission resolves granted', async () => {
  const { window } = notificationEnvironment();
  assert.equal(window.Notification.permission, 'granted');
  assert.equal(await window.Notification.requestPermission(), 'granted');
  let seen;
  await window.Notification.requestPermission((value) => { seen = value; });
  assert.equal(seen, 'granted');
});

test('a click from the app reaches onclick and the listeners, and focuses the window', async () => {
  const { window, calls } = notificationEnvironment();
  const first = new window.Notification('A');
  const second = new window.Notification('B');
  const clicks = [];
  first.onclick = (event) => clicks.push(['first', event.type]);
  second.addEventListener('click', () => clicks.push(['second']));
  const id = calls[1][1].id;
  window.__wrapperNotificationClick(id);
  assert.deepEqual(clicks, [['second']]);
  assert.equal(window.focused, 1);
  window.__wrapperNotificationClick('999'); // unknown ids are ignored
  assert.equal(window.focused, 2);
  window.__wrapperNotificationClick(calls[0][1].id);
  assert.deepEqual(clicks, [['second'], ['first', 'click']]);
});

test('a failing native call dispatches error, not show', async () => {
  const { window } = notificationEnvironment({ failing: true });
  const events = [];
  const notification = new window.Notification('x');
  notification.onerror = () => events.push('error');
  notification.onshow = () => events.push('show');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(events, ['error']);
});

test('the constructor needs a title, and options may be junk', () => {
  const { window, calls } = notificationEnvironment();
  assert.throws(() => new window.Notification(), TypeError);
  new window.Notification('t', null);
  new window.Notification(5, 'junk');
  assert.deepEqual(calls.map(([, args]) => [args.title, args.body]), [['t', ''], ['5', '']]);
});

test('the page cannot reach the click hook through enumeration, and Notification is replaceable', () => {
  const { window } = notificationEnvironment();
  assert.equal(Object.keys(window).includes('__wrapperNotificationClick'), false);
  window.Notification = function Replacement() {};
  assert.equal(window.Notification.name, 'Replacement');
});

test('only a bounded number of notifications stays clickable', () => {
  const { window, calls } = notificationEnvironment();
  const clicked = [];
  for (let i = 0; i < 250; i += 1) {
    const n = new window.Notification('n' + i);
    n.onclick = () => clicked.push(i);
  }
  window.__wrapperNotificationClick(calls[0][1].id); // the oldest was dropped
  window.__wrapperNotificationClick(calls[249][1].id);
  assert.deepEqual(clicked, [249]);
});

test('setAppBadge and clearAppBadge map to a label', async () => {
  const { navigator, calls } = notificationEnvironment();
  await navigator.setAppBadge(3);
  await navigator.setAppBadge();
  await navigator.setAppBadge(0);
  await navigator.setAppBadge(7.9);
  await navigator.setAppBadge(1e9);
  await navigator.clearAppBadge();
  assert.deepEqual(calls.map(([command, args]) => [command, args.label]), [
    ['wrapper_badge', '3'], ['wrapper_badge', '•'], ['wrapper_badge', null],
    ['wrapper_badge', '7'], ['wrapper_badge', '99999'], ['wrapper_badge', null],
  ]);
});
