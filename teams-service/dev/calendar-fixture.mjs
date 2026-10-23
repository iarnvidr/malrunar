// Made-up events, backed by the same calendar service as production. No
// Microsoft access is used by this fixture, including writes from the UI.
import { createCalendar } from '../src/calendar.mjs';
import { GraphError } from '../src/calendar-auth.mjs';

export async function createCalendarFixture(authState) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const at = hour => new Date(today.getTime() + hour * 3600000).toISOString();
  const raw = (id, subject, hour, duration = 1, patch = {}) => ({ id, subject,
    start: { dateTime: at(hour), timeZone: 'UTC' }, end: { dateTime: at(hour + duration), timeZone: 'UTC' },
    body: { contentType: 'text', content: 'Fixture event description' }, bodyPreview: 'Fixture event description',
    isOrganizer: true, organizer: { emailAddress: { name: 'Alex Example', address: 'alex@example.invalid' } }, attendees: [], ...patch });
  const events = new Map([
    ['invitation', raw('invitation', 'Meeting invitation', 15, 1, { isOrganizer: false,
      organizer: { emailAddress: { name: 'Anna Example', address: 'anna@example.invalid' } }, responseStatus: { response: 'notResponded' } })],
    ['team', raw('team', 'Weekly project meeting', 9, 1, { location: { displayName: 'Conference room' },
      onlineMeeting: { joinUrl: 'https://teams.microsoft.com/l/meetup-join/fixture' }, type: 'occurrence', seriesMasterId: 'series1',
      attendees: [{ emailAddress: { name: 'Anna Example', address: 'anna@example.invalid' }, status: { response: 'accepted' } }] })],
    ['overlap', raw('overlap', 'Overlapping planning session', 9.5)],
    ['focus', raw('focus', 'Focus time', 13, 2, { showAs: 'busy' })],
    ['day', raw('day', 'All-day preparation', 0, 24, { isAllDay: true, originalStartTimeZone: 'W. Europe Standard Time', originalEndTimeZone: 'W. Europe Standard Time' })],
    ['dst', { ...raw('dst', 'DST holiday', 0), isAllDay: true, originalStartTimeZone: 'W. Europe Standard Time', originalEndTimeZone: 'W. Europe Standard Time',
      start: { dateTime: '2026-03-28T23:00:00', timeZone: 'UTC' }, end: { dateTime: '2026-03-29T22:00:00', timeZone: 'UTC' } }]
  ]);
  const subscribed = new Map([
    ['birthdays-fixture', new Map([['birthday', raw('birthday', 'Anna’s birthday', 0, 24, { isAllDay: true, originalStartTimeZone: 'W. Europe Standard Time', originalEndTimeZone: 'W. Europe Standard Time' })]])],
    ['canteen-fixture', new Map([['lunch', raw('lunch', 'Canteen menu: soup and rye bread', 11, 1, { location: { displayName: 'Canteen' } })]])]
  ]);
  const metadata = [
    { id: 'default-fixture', name: 'Calendar', isDefaultCalendar: true, canEdit: true },
    { id: 'birthdays-fixture', name: 'Birthdays', canEdit: false },
    { id: 'canteen-fixture', name: 'canteen', canEdit: false }
  ];
  const rows = { calendar_events: [], state: [] }, removed = [];
  let nextId = 100;
  const pb = {
    listAll: async name => structuredClone(rows[name]), first: async name => structuredClone(rows[name][0] || null),
    create: async (name, data) => { const row = { ...data, id: String(++nextId) }; rows[name].push(row); return structuredClone(row); },
    update: async (name, id, data) => { const row = rows[name].find(row => row.id === id); Object.assign(row, data); return structuredClone(row); },
    remove: async (name, id) => { rows[name] = rows[name].filter(row => row.id !== id); }
  };
  const graph = {
    status: () => ({ state: authState.state, writable: authState.state === 'ok' }),
    async request(method, path, { body } = {}) {
      if (authState.state !== 'ok') throw new GraphError('Teams sign-in is required; changes were not queued', 401);
      if (method === 'GET' && path.startsWith('/me/calendars?')) return { value: metadata };
      const calendarId = path.match(/^\/me\/calendars\/([^/]+)\//)?.[1];
      if (method === 'GET') {
        const source = calendarId ? subscribed.get(decodeURIComponent(calendarId)) || new Map() : events;
        return { value: [...source.values(), ...(calendarId ? [] : removed.splice(0))], '@odata.deltaLink': `https://graph.microsoft.com/v1.0${path.split('?')[0]}?fixture=1` };
      }
      const response = path.match(/^\/me\/events\/([^/]+)\/(accept|decline)$/);
      if (method === 'POST' && response) {
        const id = decodeURIComponent(response[1]);
        if (response[2] === 'decline') { events.delete(id); removed.push({ id, '@removed': { reason: 'deleted' } }); }
        else events.get(id).responseStatus = { response: 'accepted' };
        return null;
      }
      const id = method === 'POST' ? `fixture${++nextId}` : decodeURIComponent(path.split('/').at(-1));
      if (method === 'DELETE') { events.delete(id); removed.push({ id, '@removed': { reason: 'deleted' } }); return null; }
      const event = { ...(events.get(id) || raw(id, body.subject, 10)), ...body, id };
      if (body.start?.timeZone === 'W. Europe Standard Time') {
        event.originalStartTimeZone = event.originalEndTimeZone = 'UTC';
      }
      if (body.body) event.bodyPreview = body.body.content;
      events.set(id, event); return structuredClone(event);
    }
  };
  const calendar = createCalendar({ pb, graph }); await calendar.init(); await calendar.sync(); return calendar;
}
