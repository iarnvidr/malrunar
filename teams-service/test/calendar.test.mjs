import test from 'node:test';
import assert from 'node:assert/strict';
import { createCalendar, normalizeEvent } from '../src/calendar.mjs';
import { createGraphAuth, createGraphClient, GraphError } from '../src/calendar-auth.mjs';

const NOW = Date.parse('2026-10-13T12:00:00Z');
const event = (id = 'one', patch = {}) => ({ id, subject: 'A meeting',
  start: { dateTime: '2026-10-13T09:00:00.0000000', timeZone: 'UTC' },
  end: { dateTime: '2026-10-13T10:00:00.0000000', timeZone: 'UTC' },
  body: { contentType: 'html', content: '<p>Hello</p>' }, bodyPreview: 'Hello', ...patch });
const delta = (value, cursor = 'https://graph.microsoft.com/v1.0/me/calendarView/delta?cursor=1') => ({ value, '@odata.deltaLink': cursor });

class FakePB {
  constructor() { this.rows = { calendar_events: [], state: [] }; this.index = 0; }
  async listAll(name) { return structuredClone(this.rows[name]); }
  async first(name) { return structuredClone(this.rows[name][0] || null); }
  async create(name, row) { const value = { ...structuredClone(row), id: `row${++this.index}` }; this.rows[name].push(value); return structuredClone(value); }
  async update(name, id, patch) { const row = this.rows[name].find(row => row.id === id); Object.assign(row, structuredClone(patch)); return structuredClone(row); }
  async remove(name, id) { this.rows[name] = this.rows[name].filter(row => row.id !== id); }
}
const makeGraph = pages => ({ state: { state: 'ok', writable: true }, calls: [],
  status() { return this.state; },
  async request(method, path, options) { this.calls.push({ method, path, options }); const next = pages.shift(); if (next instanceof Error) throw next; return next; } });
const make = async (pages, pb = new FakePB(), metadata = [{ id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true }]) => {
  const graph = makeGraph([{ value: metadata }, ...pages]), calendar = createCalendar({ pb, graph, now: () => NOW, timeZone: 'W. Europe Standard Time' }); await calendar.init(); return { calendar, graph, pb };
};
const range = { start: '2026-10-12T00:00:00Z', end: '2026-10-19T00:00:00Z' };

test('full pages, delta edits and deletes survive a restart without Microsoft', async () => {
  const { calendar, graph, pb } = await make([
    { value: [event('one')], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/calendarView/delta?skip=2' },
    delta([event('two')]), delta([event('one', { subject: 'Changed' }), { id: 'two', '@removed': { reason: 'deleted' } }])
  ]);
  await calendar.sync(); assert.equal(calendar.list(range).length, 2);
  await calendar.sync(); assert.equal(calendar.get('one').subject, 'Changed'); assert.equal(calendar.get('two'), null);
  assert.match(graph.calls[2].path, /skip=2/); assert.match(graph.calls[3].path, /cursor=1/);
  const again = await make([new GraphError('Signed out', 401)], pb);
  again.graph.state = { state: 'signed-out', writable: false };
  await assert.rejects(again.calendar.sync(), /Signed out/);
  assert.equal(again.calendar.list(range)[0].subject, 'Changed'); assert.equal(again.calendar.status().writable, false);
});

test('interrupted full refresh retains cached events; successful reset reconciles missing ones', async () => {
  const { calendar, graph } = await make([delta([event('one'), event('two')]), new GraphError('Expired delta', 410),
    { value: [event('one')], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/calendarView/delta?skip=next' },
    new GraphError('No network', 503), delta([])]);
  await calendar.sync();
  await assert.rejects(calendar.sync(), /No network/); assert.equal(calendar.list(range).length, 2);
  await calendar.sync(); assert.equal(calendar.get('two'), null); assert.equal(calendar.get('one').subject, 'A meeting');
  assert.match(graph.calls.at(-1).path, /skip=next/);
});

test('tombstones outside the tracked range do not delete an archived event', async () => {
  const { calendar } = await make([delta([event('old', {
    start: { dateTime: '1990-10-13T09:00:00' }, end: { dateTime: '1990-10-13T10:00:00' }
  })]), delta([{ id: 'old', '@removed': { reason: 'deleted' } }])]);
  await calendar.sync(); await calendar.sync(); assert.ok(calendar.get('old'));
});

test('recurring instances and all-day boundaries retain DST dates in the zone they were made in', () => {
  const normalized = normalizeEvent(event('day', { isAllDay: true, originalStartTimeZone: 'W. Europe Standard Time',
    originalEndTimeZone: 'W. Europe Standard Time', start: { dateTime: '2026-03-28T23:00:00' }, end: { dateTime: '2026-03-29T22:00:00' },
    type: 'exception', seriesMasterId: 'series' }));
  assert.equal(normalized.allDayStart, '2026-03-29'); assert.equal(normalized.allDayEnd, '2026-03-30');
  assert.equal(normalized.type, 'exception'); assert.equal(normalized.seriesMasterId, 'series');
  assert.equal(normalizeEvent(event('unsafe', { onlineMeeting: { joinUrl: 'javascript:alert(1)' } })).joinUrl, '');
});

test('create/update/delete are live, recurrence edits target this instance, failed writes retain cache', async () => {
  const { calendar, graph } = await make([delta([event('occurrence', { type: 'occurrence' })]),
    event('new'), event('occurrence', { subject: 'Renamed', type: 'exception' }), new GraphError('Sign in', 401), null]);
  await calendar.sync();
  await calendar.create({ subject: 'New', start: '2026-10-13T11:00:00Z', end: '2026-10-13T12:00:00Z', attendees: [] });
  assert.ok(graph.calls[2].options.body.transactionId); assert.deepEqual(graph.calls[2].options.body.attendees, []);
  await calendar.update('occurrence', { subject: 'Renamed' });
  assert.equal(graph.calls[3].method, 'PATCH'); assert.equal(graph.calls[3].path, '/me/events/occurrence');
  assert.deepEqual(graph.calls[3].options.body, { subject: 'Renamed' });
  await assert.rejects(calendar.remove('new'), /Sign in/); assert.ok(calendar.get('new'));
  await calendar.remove('new'); assert.equal(calendar.get('new'), null);
  await assert.rejects(calendar.create({ subject: 'Bad', start: '2026-10-13', end: '2026-10-12' }), /end after/);
});

test('all-day creation uses local midnight and no attendee is added implicitly', async () => {
  const { calendar, graph } = await make([delta([]), event('day', { isAllDay: true })]); await calendar.sync();
  await calendar.create({ subject: 'Holiday', isAllDay: true, start: '2026-03-29', end: '2026-03-30' });
  assert.deepEqual(graph.calls[2].options.body.start, { dateTime: '2026-03-29T00:00:00', timeZone: 'W. Europe Standard Time' });
  assert.equal(graph.calls[2].options.body.attendees, undefined);
});

test('availability and privacy round-trip through create, edit and the cache', async () => {
  const raw = event('private', { showAs: 'oof', sensitivity: 'private' });
  const { calendar, graph } = await make([delta([raw]), raw, raw]);
  await calendar.sync();
  assert.equal(calendar.get('private').showAs, 'oof'); assert.equal(calendar.get('private').sensitivity, 'private');
  await calendar.create({ subject: 'Away', start: range.start, end: range.end, showAs: 'oof', sensitivity: 'private' });
  assert.equal(graph.calls.at(-1).options.body.showAs, 'oof');
  assert.equal(graph.calls.at(-1).options.body.sensitivity, 'private');
  await calendar.update('private', { showAs: 'oof', sensitivity: 'private' });
  assert.deepEqual(graph.calls.at(-1).options.body, { showAs: 'oof', sensitivity: 'private' });
  assert.equal(calendar.get('private').sensitivity, 'private');
  for (const input of [{ showAs: 'invalid' }, { sensitivity: false }])
    await assert.rejects(calendar.update('private', input), error => error.status === 400);
});

test('meeting responses send to the organizer, preserve failed writes and remove declined invitations', async () => {
  const invitation = event('invite/id', { organizer: { emailAddress: { address: 'host@example.org' } }, responseStatus: { response: 'notResponded' }, type: 'occurrence' });
  const { calendar, graph, pb } = await make([delta([invitation]), new GraphError('Offline', 503), null, null]);
  await calendar.sync();
  await assert.rejects(calendar.respond('invite/id', { response: 'accept' }), /Offline/);
  assert.equal(calendar.get('invite/id').responseStatus.response, 'notResponded');
  await calendar.respond('invite/id', { response: 'accept' });
  assert.equal(graph.calls.at(-1).path, '/me/events/invite%2Fid/accept');
  assert.deepEqual(graph.calls.at(-1).options.body, { sendResponse: true });
  assert.equal(calendar.get('invite/id').responseStatus.response, 'accepted');
  assert.equal(pb.rows.calendar_events[0].event.responseStatus.response, 'accepted');
  await calendar.respond('invite/id', { response: 'decline' });
  assert.equal(graph.calls.at(-1).path, '/me/events/invite%2Fid/decline');
  assert.equal(calendar.get('invite/id'), null); assert.equal(pb.rows.calendar_events.length, 0);
});

test('meeting responses reject invalid actions, organizers, cancelled events and read-only calendars', async () => {
  const organizer = { emailAddress: { address: 'host@example.org' } };
  const metadata = [{ id: 'primary', name: 'Calendar', canEdit: true, isDefaultCalendar: true }, { id: 'readonly', name: 'Shared', canEdit: false }];
  const { calendar, graph } = await make([delta([event('own', { organizer, isOrganizer: true }), event('cancelled', { organizer, isCancelled: true }), event('appointment')]), delta([event('shared', { organizer })])], new FakePB(), metadata);
  await calendar.sync();
  const calls = graph.calls.length;
  for (const id of ['own', 'cancelled', 'appointment']) await assert.rejects(calendar.respond(id, { response: 'accept' }), error => error.status === 400);
  await assert.rejects(calendar.respond('own', { response: 'delete' }), error => error.status === 400);
  await assert.rejects(calendar.respond('absent', { response: 'accept' }), error => error.status === 404);
  await assert.rejects(calendar.respond(calendar.list(range).find(value => value.calendarId === 'readonly').key, { response: 'decline' }), error => error.status === 403);
  assert.equal(graph.calls.length, calls);
});

test('Graph accepts empty 202 meeting response bodies', async () => {
  const graph = createGraphClient({ graphAuth: { token: async () => 'secret', status: () => ({ writable: true }) },
    fetchImpl: async () => new Response(null, { status: 202 }) });
  assert.equal(await graph.request('POST', '/me/events/one/accept', { body: { sendResponse: true } }), null);
});

test('older cached events refresh privacy and response fields without losing offline reads', async () => {
  const { calendar, pb } = await make([delta([event('old')])]);
  await calendar.sync();
  delete pb.rows.calendar_events[0].event.sensitivity;
  delete pb.rows.calendar_events[0].event.responseStatus;
  const next = await make([delta([event('old', { sensitivity: 'private', responseStatus: { response: 'accepted' } })])], pb);
  assert.ok(next.calendar.get('old'));
  await next.calendar.sync();
  assert.match(next.graph.calls[1].path, /startDateTime=/);
  assert.equal(next.calendar.get('old').sensitivity, 'private');
  assert.equal(next.calendar.get('old').responseStatus.response, 'accepted');
});

test('series masters expand paged occurrences and re-expansion reconciles cancelled instances', async () => {
  const master = event('series', { type: 'seriesMaster' });
  const occurrence = id => event(id, { type: 'occurrence', seriesMasterId: 'series' });
  const { calendar, graph } = await make([delta([master]),
    { value: [occurrence('first')], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/events/series/instances?skip=2' },
    { value: [occurrence('second')] }, delta([master]), { value: [occurrence('second')] },
    delta([{ id: 'series', '@removed': { reason: 'deleted' } }])]);
  await calendar.sync(); assert.deepEqual(calendar.list(range).map(value => value.id), ['first', 'second']);
  assert.match(graph.calls[2].path, /\/events\/series\/instances\?/);
  await calendar.sync(); assert.equal(calendar.get('first'), null); assert.ok(calendar.get('second'));
  await calendar.sync(); assert.equal(calendar.list(range).length, 0);
});

test('failed series expansion preserves already cached instances', async () => {
  const master = event('series', { type: 'seriesMaster' });
  const { calendar } = await make([delta([master]), { value: [event('first', { type: 'occurrence', seriesMasterId: 'series' })] },
    delta([master]), { value: [], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/events/series/instances?skip=2' }, new GraphError('Network gone', 503)]);
  await calendar.sync(); await assert.rejects(calendar.sync(), /Network gone/); assert.ok(calendar.get('first'));
});

test('subscribed calendars keep separate cursors and event keys; read-only writes are rejected server-side', async () => {
  const metadata = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'birthdays-id', name: 'Birthdays', canEdit: false },
    { id: 'canteen-id', name: 'canteen', canEdit: false }
  ];
  const { calendar, graph, pb } = await make([
    delta([event('same')]), delta([event('same', { subject: 'Birthday' })]), delta([event('same', { subject: 'Lunch' })])
  ], new FakePB(), metadata);
  await calendar.sync();
  assert.equal(graph.calls[0].path, '/me/calendars?$select=id,name,canEdit,isDefaultCalendar');
  const [primary, birthday, lunch] = calendar.list(range);
  assert.equal(primary.id, 'same'); assert.equal(primary.key, 'same');
  assert.equal(birthday.calendarName, 'Birthdays'); assert.equal(lunch.calendarName, 'canteen');
  assert.notEqual(birthday.key, lunch.key); assert.notEqual(birthday.key, birthday.id);
  assert.match(graph.calls[2].path, /\/calendars\/birthdays-id\/calendarView\/delta/);
  assert.match(graph.calls[3].path, /\/calendars\/canteen-id\/calendarView\/delta/);
  assert.equal(pb.rows.calendar_events.length, 3);
  const writesBefore = graph.calls.length;
  await assert.rejects(calendar.update(birthday.key, { subject: 'Forged', calendarId: 'default' }), /read-only/);
  await assert.rejects(calendar.remove(lunch.key), /read-only/);
  await assert.rejects(calendar.create({ subject: 'Forged', start: '2026-10-13T11:00:00Z', end: '2026-10-13T12:00:00Z', calendarId: 'canteen-id' }), /read-only/);
  assert.equal(graph.calls.length, writesBefore);
  const restored = await make([new GraphError('signed out', 401)], pb);
  restored.graph.state = { state: 'signed-out', writable: false };
  assert.equal(restored.calendar.get(birthday.key).calendarName, 'Birthdays');
  assert.equal(restored.calendar.status().calendars.find(item => item.id === 'canteen-id').canEdit, false);
});

test('unsupported subscribed delta falls back to a complete snapshot and retains cache on a failed page', async () => {
  const metadata = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'cantine', name: 'canteen', canEdit: false }
  ];
  const { calendar, graph } = await make([
    delta([]), new GraphError('Not supported', 404),
    { value: [event('lunch')] },
    delta([]), new GraphError('Network gone', 503)
  ], new FakePB(), metadata);
  await calendar.sync();
  assert.equal(calendar.list(range).length, 1);
  const lunch = calendar.list(range)[0];
  assert.match(graph.calls[3].path, /\/calendars\/cantine\/calendarView\?/);
  await assert.rejects(calendar.sync(), /Network gone/);
  assert.ok(calendar.get(lunch.key));
});

test('one subscribed calendar access denial leaves default calendar reads and writes available', async () => {
  const metadata = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'private-id', name: 'canteen', canEdit: false }
  ];
  const { calendar, graph } = await make([delta([event('primary')]), new GraphError('Access denied', 403, 'ErrorAccessDenied'), event('created')], new FakePB(), metadata);
  await calendar.sync();
  const state = calendar.status();
  assert.equal(state.state, 'ok'); assert.equal(state.writable, true);
  assert.equal(state.calendars.find(value => value.id === 'private-id').accessible, false);
  assert.equal(state.calendars.find(value => value.id === 'private-id').writable, false);
  assert.equal(calendar.status().calendars.find(value => value.id === 'private-id').subscribed, true);
  await calendar.create({ subject: 'Allowed', start: '2026-10-13T11:00:00Z', end: '2026-10-13T12:00:00Z' });
  assert.equal(graph.calls.at(-1).path, '/me/events');
});

test('Graph canEdit is mapped strictly; an omitted permission never becomes writable', async () => {
  const metadata = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'unknown-permission', name: 'Shared' }
  ];
  const { calendar } = await make([delta([event('primary')]), delta([event('shared')])], new FakePB(), metadata);
  await calendar.sync();
  const shared = calendar.list(range).find(value => value.calendarId === 'unknown-permission');
  assert.equal(shared.calendarWritable, false);
  assert.equal(calendar.status().calendars.find(value => value.id === 'unknown-permission').canEdit, false);
  await assert.rejects(calendar.create({ subject: 'Forged', calendarId: 'unknown-permission', start: '2026-10-13T11:00:00Z', end: '2026-10-13T12:00:00Z' }), /read-only/);
});

test('removed subscriptions discard cached events and cursors and are no longer polled', async () => {
  let now = NOW;
  const pb = new FakePB(), calls = [];
  const both = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'subscribed-id', name: 'Shared archive', canEdit: false }
  ];
  const graph = makeGraph([]);
  graph.request = async (method, path, options) => {
    calls.push({ method, path, options });
    if (path.startsWith('/me/calendars?')) return { value: now === NOW ? both : [both[0]] };
    if (path.includes('/calendars/subscribed-id/')) return delta([event('archived-item')]);
    return delta([event('primary-item')]);
  };
  const calendar = createCalendar({ pb, graph, now: () => now }); await calendar.init();
  await calendar.sync();
  assert.equal(calendar.list(range).some(value => value.id === 'archived-item'), true);
  assert.ok(pb.rows.state[0].v.calendars['subscribed-id']);
  now += 16 * 60 * 1000;
  await calendar.sync();
  const source = calendar.status().calendars.find(value => value.id === 'subscribed-id');
  assert.equal(source, undefined);
  assert.equal(calendar.list(range).some(value => value.id === 'archived-item'), false);
  assert.equal(pb.rows.calendar_events.some(value => value.calendar_id === 'subscribed-id'), false);
  assert.equal(pb.rows.state[0].v.calendars['subscribed-id'], undefined);
  assert.equal(calls.filter(value => value.path.includes('/calendars/subscribed-id/')).length, 1);
  assert.equal(calendar.status().writable, true);
});

test('an inaccessible subscribed source recovers access and persists the recovered metadata', async () => {
  const metadata = [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'private-id', name: 'Shared archive', canEdit: false }
  ];
  const { calendar, pb } = await make([delta([]), new GraphError('Access denied', 403, 'ErrorAccessDenied'), delta([]), delta([event('recovered')])], new FakePB(), metadata);
  await calendar.sync(); assert.equal(calendar.status().calendars.find(value => value.id === 'private-id').accessible, false);
  assert.equal(pb.rows.state[0].v.calendars['private-id'].full, true);
  await calendar.sync();
  assert.equal(calendar.status().calendars.find(value => value.id === 'private-id').accessible, true);
  assert.equal(pb.rows.state[0].v.calendars['private-id'].metadata.accessible, true);
  assert.equal(pb.rows.state[0].v.calendars['private-id'].full, false);
});

test('legacy default cursor and cached records upgrade without a destructive full refresh', async () => {
  const pb = new FakePB();
  pb.rows.calendar_events.push({ id: 'old-row', event_id: 'old', start_at: NOW, end_at: NOW + 3600000, generation: 'old-gen',
    event: normalizeEvent(event('old')) });
  pb.rows.state.push({ id: 'state-row', k: 'calendar_delta', v: { version: 2, from: '2000-01-01T00:00:00Z', to: '2029-01-01T00:00:00Z',
    link: 'https://graph.microsoft.com/v1.0/me/calendarView/delta?cursor=legacy', full: false, generation: 'old-gen', syncedAt: NOW } });
  const { calendar, graph } = await make([delta([]), delta([]), delta([])], pb, [
    { id: 'graph-default', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'birthdays-id', name: 'Birthdays', canEdit: false }, { id: 'canteen-id', name: 'canteen', canEdit: false }
  ]);
  await calendar.sync();
  assert.ok(calendar.get('old'));
  assert.match(graph.calls[1].path, /cursor=legacy/);
  assert.match(pb.rows.state[0].v.calendars.default.link, /cursor=1/);
  assert.equal(pb.rows.calendar_events[0].event_id, 'old');
});

const jwt = exp => `e30.${Buffer.from(JSON.stringify({ exp })).toString('base64url')}.x`;
test('Graph renews through existing session client and keeps rotated secrets in memory', async () => {
  const storage = [{ value: JSON.stringify({ credentialType: 'RefreshToken', clientId: '5e3ce6c0-2b1f-4285-8d4b-75ee78787346', secret: 'existing' }) },
    { value: JSON.stringify({ credentialType: 'AccessToken', realm: 'tenant-id', target: 'other', secret: 'irrelevant' }) }];
  const original = JSON.stringify(storage), calls = [];
  const graphAuth = createGraphAuth({ lib: { sessionStore: { readSessionState: () => ({}), getTeamsOrigin: () => ({ localStorage: storage }) } },
    auth: { ensure: async () => true }, now: () => NOW, fetchImpl: async (url, options) => {
      calls.push({ url, options }); return Response.json({ access_token: jwt(NOW / 1000 + 3600), refresh_token: 'rotated' });
    } });
  await Promise.all([graphAuth.token(), graphAuth.token()]); assert.equal(calls.length, 1);
  assert.equal(new URLSearchParams(calls[0].options.body).get('refresh_token'), 'existing');
  assert.equal(calls[0].options.headers.Origin, 'https://teams.microsoft.com');
  await graphAuth.token({ force: true }); assert.equal(new URLSearchParams(calls[1].options.body).get('refresh_token'), 'rotated');
  assert.equal(JSON.stringify(storage), original); assert.equal(graphAuth.status().writable, true);
});

test('Graph rejects foreign delta links and never retries a write on authentication failure', async () => {
  let calls = 0;
  const graphAuth = { token: async () => 'secret', status: () => ({ writable: true }) };
  const graph = createGraphClient({ graphAuth, fetchImpl: async () => { calls++; return Response.json({}, { status: 401 }); } });
  await assert.rejects(graph.request('GET', 'https://attacker.test/v1.0/me/events'), /outside/); assert.equal(calls, 0);
  await assert.rejects(graph.request('POST', '/me/events', { body: {} }), error => error.signedOut);
  assert.equal(calls, 1); await assert.rejects(graph.request('GET', '/me/events', { priority: 20 }), /priority/);
});

test('calendar access-denied 403 does not mark the shared Teams session signed out', async () => {
  const authState = { state: 'ok', writable: true };
  const graphAuth = { token: async () => 'secret', status: () => authState,
    fail(error) { if (error.signedOut) authState.state = 'signed-out'; else authState.state = 'unreachable'; } };
  const graph = createGraphClient({ graphAuth, fetchImpl: async () => Response.json({ error: { code: 'ErrorAccessDenied', message: 'Access denied' } }, { status: 403 }) });
  await assert.rejects(graph.request('GET', '/me/calendars/private/calendarView/delta'), error => error.status === 403 && !error.signedOut);
  assert.deepEqual(authState, { state: 'ok', writable: true });
});

test('a missing Teams session is signed out, without contacting a new login service', async () => {
  const graphAuth = createGraphAuth({ lib: { sessionStore: { readSessionState: () => null } },
    auth: { ensure: async () => false }, fetchImpl: async () => assert.fail('must not contact Microsoft') });
  await assert.rejects(graphAuth.token(), error => error.signedOut);
  assert.equal(graphAuth.status().state, 'signed-out'); assert.equal(graphAuth.status().writable, false);
});
