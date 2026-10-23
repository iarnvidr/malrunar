// Graph calendarView/delta returns recurring occurrences and exceptions, as
// well as appointments/all-day events. Reads below never require Microsoft.
// https://learn.microsoft.com/en-us/graph/api/event-delta?view=graph-rest-1.0
import crypto from 'node:crypto';
import { quote } from './pb.mjs';
import { GraphError } from './calendar-auth.mjs';

const STATE_KEY = 'calendar_delta';
const DEFAULT_CALENDAR = 'default';
// Windows time zone names that Graph reports, mapped to the IANA names Intl knows.
const zones = { 'Romance Standard Time': 'Europe/Copenhagen', 'W. Europe Standard Time': 'Europe/Berlin',
  'GMT Standard Time': 'Europe/London', 'Eastern Standard Time': 'America/New_York', 'UTC': 'UTC' };
// The zone of all-day events the service creates and of events that carry no zone: the time zone
// of the process (TZ), or MALRUNAR_TEAMS_SERVICE_TIME_ZONE (a Windows or IANA name), else UTC.
export const defaultTimeZone = () => process.env.MALRUNAR_TEAMS_SERVICE_TIME_ZONE
  || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const timestamp = value => Date.parse(/Z$|[+-]\d\d:\d\d$/.test(value || '') ? value : `${value}Z`);
const dateInZone = (ts, zone) => {
  try { return new Intl.DateTimeFormat('en-CA', { timeZone: zones[zone] || zone || zones[defaultTimeZone()] || defaultTimeZone(),
    year: 'numeric', month: '2-digit', day: '2-digit' }).format(ts); }
  catch { return new Date(ts).toISOString().slice(0, 10); }
};

export function normalizeEvent(raw, calendar = { id: DEFAULT_CALENDAR, name: 'Calendar', canEdit: true }) {
  const start = timestamp(raw.start?.dateTime), end = timestamp(raw.end?.dateTime);
  if (!raw.id || !Number.isFinite(start) || !Number.isFinite(end)) throw new GraphError('Microsoft returned an event with invalid dates', 502);
  const join = raw.onlineMeeting?.joinUrl || raw.onlineMeetingUrl || '';
  return { id: raw.id, calendarId: calendar.id, calendarName: calendar.name,
    calendarWritable: calendar.canEdit === true, subject: raw.subject || '(No title)', start, end, isAllDay: !!raw.isAllDay,
    allDayStart: raw.isAllDay ? dateInZone(start, raw.originalStartTimeZone) : '',
    allDayEnd: raw.isAllDay ? dateInZone(end, raw.originalEndTimeZone || raw.originalStartTimeZone) : '',
    cancelled: !!raw.isCancelled, showAs: raw.showAs || 'busy', sensitivity: raw.sensitivity || 'normal',
    responseStatus: raw.responseStatus || { response: 'notResponded' }, location: raw.location?.displayName || '',
    body: raw.body?.content || '', bodyType: raw.body?.contentType || 'text', bodyPreview: raw.bodyPreview || '',
    organizer: raw.organizer?.emailAddress || null, attendees: raw.attendees || [],
    isOrganizer: !!raw.isOrganizer, type: raw.type || 'singleInstance', seriesMasterId: raw.seriesMasterId || '',
    joinUrl: /^https:\/\//i.test(join) ? join : '', webLink: /^https:\/\//i.test(raw.webLink || '') ? raw.webLink : '',
    timeZone: raw.originalStartTimeZone || 'UTC', lastModified: raw.lastModifiedDateTime || '' };
}

function eventBody(input, existing = null, timeZone = defaultTimeZone()) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new GraphError('An event is required', 400);
  const allowed = ['subject', 'start', 'end', 'isAllDay', 'location', 'body', 'attendees', 'showAs', 'sensitivity'];
  if (Object.keys(input).some(key => !allowed.includes(key))) throw new GraphError('Unsupported event field', 400);
  const out = {};
  for (const [field, values] of Object.entries({ showAs: ['free', 'tentative', 'busy', 'oof', 'workingElsewhere', 'unknown'],
    sensitivity: ['normal', 'personal', 'private', 'confidential'] })) {
    if (field in input) {
      if (!values.includes(input[field])) throw new GraphError(`Invalid ${field}`, 400);
      out[field] = input[field];
    }
  }
  if ('subject' in input || !existing) {
    if (typeof input.subject !== 'string' || !input.subject.trim() || input.subject.length > 1000) throw new GraphError('Enter an event title (up to 1000 characters)', 400);
    out.subject = input.subject.trim();
  }
  const start = input.start === undefined ? existing?.start : Date.parse(input.start);
  const end = input.end === undefined ? existing?.end : Date.parse(input.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) throw new GraphError('The event must end after it starts', 400);
  if ('start' in input || !existing) out.start = { dateTime: new Date(start).toISOString(), timeZone: 'UTC' };
  if ('end' in input || !existing) out.end = { dateTime: new Date(end).toISOString(), timeZone: 'UTC' };
  if ('isAllDay' in input) {
    if (typeof input.isAllDay !== 'boolean') throw new GraphError('All-day must be true or false', 400);
    out.isAllDay = input.isAllDay;
  }
  // All-day requests use midnight in the UI's chosen local date, represented
  // with the configured time zone so Graph's all-day invariants survive DST.
  if (input.isAllDay || (existing?.isAllDay && input.isAllDay !== false)) {
    for (const key of ['start', 'end']) {
      if (input[key] !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(input[key])) throw new GraphError('All-day events need date-only start and end', 400);
      if (out[key]) out[key] = { dateTime: `${input[key]}T00:00:00`, timeZone };
    }
  }
  if ('location' in input) {
    if (typeof input.location !== 'string' || input.location.length > 1000) throw new GraphError('The location is too long', 400);
    out.location = { displayName: input.location };
  }
  if ('body' in input) {
    if (typeof input.body !== 'string' || input.body.length > 100000) throw new GraphError('The description is too long', 400);
    out.body = { contentType: 'text', content: input.body };
  }
  if ('attendees' in input) {
    if (!Array.isArray(input.attendees) || input.attendees.length > 100 || input.attendees.some(email => typeof email !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))) throw new GraphError('Enter valid attendee email addresses', 400);
    out.attendees = input.attendees.map(address => ({ emailAddress: { address }, type: 'required' }));
  }
  return out;
}

export function createCalendar({ pb, graph, now = () => Date.now(), historyStart = '2000-01-01T00:00:00Z', timeZone = defaultTimeZone(), log = () => {} }) {
  const rows = new Map();
  const calendars = new Map([[DEFAULT_CALENDAR, { id: DEFAULT_CALENDAR, graphId: '', name: 'Calendar', canEdit: true, isDefault: true }]]);
  let stateRow = null, progress = null, inflight = null, timer = null, stopped = true;
  let syncedAt = 0, detail = '', state = 'starting', calendarsDiscoveredAt = 0;
  const bounds = () => ({ from: historyStart, to: `${new Date(now()).getUTCFullYear() + 3}-01-01T00:00:00Z` });
  const initialLink = (range, calendarId) => {
    const path = calendarId === DEFAULT_CALENDAR ? '/me/calendarView/delta' :
      `/me/calendars/${encodeURIComponent(calendarMeta(calendarId)?.graphId || calendarId)}/calendarView/delta`;
    return `${path}?${new URLSearchParams({ startDateTime: range.from, endDateTime: range.to })}`;
  };
  const rowKey = (calendarId, eventId) => `${calendarId}\u0000${eventId}`;
  const localEventKey = (calendarId, eventId) => calendarId === DEFAULT_CALENDAR ? eventId :
    `sub:${Buffer.from(calendarId).toString('base64url')}.${Buffer.from(eventId).toString('base64url')}`;
  const calendarMeta = id => calendars.get(id) || (id === DEFAULT_CALENDAR ? calendars.get(DEFAULT_CALENDAR) : null);
  const overlaps = (event, start, end) => event.start < end && event.end > start;
  async function saveProgress() {
    stateRow = stateRow ? await pb.update('state', stateRow.id, { v: progress }) : await pb.create('state', { k: STATE_KEY, v: progress });
  }
  async function save(raw, calendarId = DEFAULT_CALENDAR, generation = '') {
    const calendar = calendarMeta(calendarId) || { id: calendarId, name: 'Calendar', canEdit: false };
    const event = { ...normalizeEvent(raw, calendar), key: localEventKey(calendarId, raw.id) };
    const key = rowKey(calendarId, event.id), previous = rows.get(key);
    const data = { event_id: event.id, calendar_id: calendarId, start_at: event.start, end_at: event.end, event,
      generation: generation || previous?.generation || '' };
    rows.set(key, previous ? await pb.update('calendar_events', previous.id, data) : await pb.create('calendar_events', data));
    return event;
  }
  async function erase(id, calendarId = DEFAULT_CALENDAR) {
    const key = rowKey(calendarId, id), previous = rows.get(key);
    if (previous) { await pb.remove('calendar_events', previous.id); rows.delete(key); }
  }
  async function expandSeries(master, range, calendarId) {
    // calendarView/delta can return a series master rather than every unchanged
    // occurrence. Expand the master through Graph so the offline view includes
    // every occurrence and exception, not only the series' original date.
    const base = calendarId === DEFAULT_CALENDAR ? `/me/events/${encodeURIComponent(master.id)}` :
      `/me/calendars/${encodeURIComponent(calendarId)}/events/${encodeURIComponent(master.id)}`;
    let link = `${base}/instances?${new URLSearchParams({ startDateTime: range.from, endDateTime: range.to })}`;
    const present = new Set();
    while (link) {
      const page = await graph.request('GET', link, { priority: 2 });
      if (!Array.isArray(page.value)) throw new GraphError('Microsoft returned invalid recurring instances', 502);
      for (const instance of page.value) {
        if (instance['@removed']) continue;
        const raw = { ...instance, seriesMasterId: instance.seriesMasterId || master.id };
        const cursor = progress.calendars[calendarId];
        await save(raw, calendarId, cursor?.full ? cursor.generation : ''); present.add(raw.id);
      }
      link = page['@odata.nextLink'];
    }
    for (const row of [...rows.values()]) if (row.calendar_id === calendarId && row.event.seriesMasterId === master.id && !present.has(row.event_id) &&
      overlaps(row.event, Date.parse(range.from), Date.parse(range.to))) await erase(row.event_id, calendarId);
  }
  async function discoverCalendars() {
    let link = '/me/calendars?$select=id,name,canEdit,isDefaultCalendar';
    const found = [];
    while (link) {
      const page = await graph.request('GET', link, { priority: 2 });
      if (!Array.isArray(page.value)) throw new GraphError('Microsoft returned invalid calendar metadata', 502);
      found.push(...page.value.filter(value => value?.id && typeof value.name === 'string'));
      link = page['@odata.nextLink'];
    }
    if (!found.length) throw new GraphError('Microsoft returned no calendars', 502);
    const primary = found.find(value => value.isDefaultCalendar) || found.find(value => value.name.toLowerCase() === 'calendar');
    const previous = new Map(calendars);
    calendars.clear();
    if (primary) calendars.set(DEFAULT_CALENDAR, { ...previous.get(DEFAULT_CALENDAR), id: DEFAULT_CALENDAR,
      graphId: primary.id, name: primary.name, canEdit: primary.canEdit === true, isDefault: true, subscribed: true });
    else calendars.set(DEFAULT_CALENDAR, { ...previous.get(DEFAULT_CALENDAR), id: DEFAULT_CALENDAR,
      graphId: previous.get(DEFAULT_CALENDAR)?.graphId || '', name: previous.get(DEFAULT_CALENDAR)?.name || 'Calendar',
      canEdit: false, accessible: false, isDefault: true, subscribed: true });
    for (const value of found) {
      if (value === primary) continue;
      calendars.set(value.id, { ...previous.get(value.id), id: value.id, graphId: value.id, name: value.name,
        canEdit: value.canEdit === true, isDefault: false, subscribed: true });
    }
    // A successful /me/calendars response is the authoritative set of sources.
    // Older code retained every removed calendar from the event cache and delta
    // state as an unavailable source. Re-adding/renaming a calendar in Outlook
    // can give it a new Graph ID, so that behavior accumulated duplicate names
    // forever. Cached events are only an offline mirror, so discard rows and
    // cursors for sources that are no longer in Microsoft's current list.
    const activeIds = new Set(calendars.keys());
    for (const [key, row] of [...rows]) if (!activeIds.has(row.calendar_id)) {
      await pb.remove('calendar_events', row.id);
      rows.delete(key);
    }
    if (progress?.calendars) for (const id of Object.keys(progress.calendars))
      if (!activeIds.has(id)) delete progress.calendars[id];
    calendarsDiscoveredAt = now();
  }
  function ensureProgress(range) {
    if (!progress || progress.version !== 3) {
      // Keep the existing default-calendar delta link and generation. This
      // upgrades the old one-calendar cursor without forcing a full refresh.
      const legacy = progress?.link ? progress : null;
      progress = { version: 3, from: range.from, to: range.to, calendars: {} };
      progress.calendars[DEFAULT_CALENDAR] = legacy ? { mode: 'delta', link: legacy.link, full: !!legacy.full,
        generation: legacy.generation || '', syncedAt: legacy.syncedAt || 0 } :
        { mode: 'delta', link: initialLink(range, DEFAULT_CALENDAR), full: true, generation: crypto.randomUUID(), syncedAt: 0 };
    }
    if (progress.from !== range.from || progress.to !== range.to) {
      progress.from = range.from; progress.to = range.to;
      for (const id of calendars.keys()) progress.calendars[id] = { mode: 'delta', link: initialLink(range, id), full: true, generation: crypto.randomUUID(), syncedAt: 0 };
    }
    for (const id of calendars.keys()) if (!progress.calendars[id]) {
      progress.calendars[id] = { mode: 'delta', link: initialLink(range, id), full: true, generation: crypto.randomUUID(), syncedAt: 0 };
    }
    for (const [id, calendar] of calendars) progress.calendars[id] = { ...progress.calendars[id], metadata: calendar };
  }
  async function syncDeltaCalendar(calendarId, range) {
    const cursor = progress.calendars[calendarId];
    let resets = 0;
    for (;;) {
      let page;
      try { page = await graph.request('GET', cursor.link, { priority: 2 }); }
      catch (error) {
        if (calendarId !== DEFAULT_CALENDAR && [400, 404, 405, 501].includes(error.status)) {
          cursor.mode = 'snapshot'; cursor.syncedAt = 0; await saveProgress();
          return syncSnapshotCalendar(calendarId, range);
        }
        if ((error.status === 410 || error.code === 'SyncStateNotFound') && resets++ < 1) {
          cursor.link = initialLink(range, calendarId); cursor.full = true; cursor.generation = crypto.randomUUID(); cursor.syncedAt = 0;
          await saveProgress(); continue;
        }
        throw error;
      }
      if (!Array.isArray(page.value)) throw new GraphError('Microsoft returned an invalid calendar change page', 502);
      for (const raw of page.value) {
        if (raw['@removed']) {
          const key = rowKey(calendarId, raw.id), previous = rows.get(key);
          if (previous && overlaps(previous.event, Date.parse(range.from), Date.parse(range.to))) {
            await erase(raw.id, calendarId);
            if (previous.event.type === 'seriesMaster') for (const row of [...rows.values()])
              if (row.calendar_id === calendarId && row.event.seriesMasterId === raw.id) await erase(row.event_id, calendarId);
          }
        } else {
          if (raw.type === 'seriesMaster') await expandSeries(raw, range, calendarId);
          await save(raw, calendarId, cursor.full ? cursor.generation : '');
        }
      }
      const next = page['@odata.nextLink'], delta = page['@odata.deltaLink'];
      if (!next && !delta) throw new GraphError('Microsoft returned no calendar change cursor', 502);
      if (!next && cursor.full) for (const row of [...rows.values()]) if (row.calendar_id === calendarId && row.generation !== cursor.generation &&
        overlaps(row.event, Date.parse(range.from), Date.parse(range.to))) await erase(row.event_id, calendarId);
      cursor.link = next || delta;
      if (!next) { cursor.syncedAt = now(); cursor.full = false; }
      await saveProgress();
      if (!next) return;
    }
  }
  async function syncSnapshotCalendar(calendarId, range) {
    const calendar = calendarMeta(calendarId);
    const query = new URLSearchParams({ startDateTime: range.from, endDateTime: range.to });
    let link = `/me/calendars/${encodeURIComponent(calendar.graphId)}/calendarView?${query}`;
    const snapshot = [];
    while (link) {
      const page = await graph.request('GET', link, { priority: 2 });
      if (!Array.isArray(page.value)) throw new GraphError('Microsoft returned an invalid calendar view', 502);
    snapshot.push(...page.value.filter(raw => !raw['@removed']));
      link = page['@odata.nextLink'];
    }
    // CalendarView returns concrete recurrence occurrences and exceptions. A
    // complete paged read is staged before touching the cached snapshot.
    const generation = crypto.randomUUID();
    const expanded = [];
    for (const raw of snapshot) {
      if (raw.type !== 'seriesMaster') { expanded.push(raw); continue; }
      let instanceLink = `/me/calendars/${encodeURIComponent(calendar.graphId)}/events/${encodeURIComponent(raw.id)}/instances?${query}`;
      while (instanceLink) {
        const page = await graph.request('GET', instanceLink, { priority: 2 });
        if (!Array.isArray(page.value)) throw new GraphError('Microsoft returned invalid recurring instances', 502);
        expanded.push(...page.value.filter(instance => !instance['@removed']).map(instance => ({ ...instance, seriesMasterId: instance.seriesMasterId || raw.id })));
        instanceLink = page['@odata.nextLink'];
      }
    }
    for (const raw of expanded) await save(raw, calendarId, generation);
    for (const row of [...rows.values()]) if (row.calendar_id === calendarId && row.generation !== generation &&
      overlaps(row.event, Date.parse(range.from), Date.parse(range.to))) await erase(row.event_id, calendarId);
    progress.calendars[calendarId] = { ...progress.calendars[calendarId], mode: 'snapshot', generation, full: false, syncedAt: now() };
    await saveProgress();
  }
  async function syncOnce() {
    const range = bounds();
    if (!calendarsDiscoveredAt || now() - calendarsDiscoveredAt >= 15 * 60 * 1000) await discoverCalendars();
    ensureProgress(range); await saveProgress();
    for (const id of calendars.keys()) {
      const calendar = calendarMeta(id);
      if (id !== DEFAULT_CALENDAR && calendar?.subscribed === false) continue;
      const cursor = progress.calendars[id];
      try {
        if (cursor.mode === 'snapshot') await syncSnapshotCalendar(id, range);
        else await syncDeltaCalendar(id, range);
        if (calendar) {
          calendar.accessible = true; calendar.detail = '';
          progress.calendars[id] = { ...progress.calendars[id], metadata: calendar };
          await saveProgress();
        }
      } catch (error) {
        if (id !== DEFAULT_CALENDAR && error.status === 403 && !error.signedOut) {
          const calendar = calendarMeta(id); calendar.accessible = false; calendar.detail = 'Microsoft denied access to this calendar';
          progress.calendars[id] = { ...progress.calendars[id], metadata: calendar };
          await saveProgress();
          continue;
        }
        throw error;
      }
    }
    syncedAt = Math.max(0, ...Object.values(progress.calendars).map(value => value.syncedAt || 0));
    state = 'ok'; detail = ''; return status();
  }
  function sync() {
    if (!inflight) inflight = syncOnce().catch(error => {
      state = error.signedOut ? 'signed-out' : 'unreachable'; detail = error.message; throw error;
    }).finally(() => { inflight = null; });
    return inflight;
  }
  function status() {
    const auth = graph.status();
    return { state: auth.state === 'signed-out' ? 'signed-out' : state, writable: !!auth.writable,
      detail: detail || auth.detail || '', syncedAt, syncing: !!inflight, count: rows.size,
      calendars: [...calendars.values()].map(value => ({ ...value, writable: !!auth.writable && value.canEdit && value.accessible !== false })),
      historyStart, historyEnd: bounds().to };
  }
  const findByLocalKey = key => [...rows.values()].find(row => (row.event.key || localEventKey(row.calendar_id || DEFAULT_CALENDAR, row.event.id)) === key);
  const get = key => findByLocalKey(key)?.event || rows.get(rowKey(DEFAULT_CALENDAR, key))?.event || null;
  async function write(method, key, input) {
    if (inflight) await inflight.catch(() => {});
    const existingRow = key ? findByLocalKey(key) : null;
    const existing = existingRow?.event || null;
    const calendarId = key ? existingRow?.calendar_id || DEFAULT_CALENDAR : String(input?.calendarId || DEFAULT_CALENDAR);
    const calendar = calendarMeta(calendarId);
    if (!calendar || !calendar.canEdit || calendar.accessible === false) throw new GraphError('This calendar is read-only or unavailable', 403);
    if (key && !existing) throw new GraphError('The cached event was not found', 404);
    const { calendarId: ignoredCalendarId, ...fields } = input || {};
    const body = eventBody(fields, existing, timeZone);
    if (!key) body.transactionId = crypto.randomUUID();
    const base = calendarId === DEFAULT_CALENDAR ? '/me/events' : `/me/calendars/${encodeURIComponent(calendarId)}/events`;
    const raw = await graph.request(method, `${base}${key ? '/' + encodeURIComponent(existing.id) : ''}`, { body, priority: 0 });
    return save(raw, calendarId);
  }
  async function tick() {
    if (stopped) return;
    try { await sync(); } catch (error) { log('warn', `Calendar sync: ${error.message}`); }
    if (!stopped) { timer = setTimeout(tick, 60000); timer.unref?.(); }
  }
  return {
    async init() {
      for (const row of await pb.listAll('calendar_events')) {
        row.calendar_id ||= row.event?.calendarId || DEFAULT_CALENDAR;
        if (!row.event.calendarId) row.event = { ...row.event, calendarId: DEFAULT_CALENDAR, calendarName: 'Calendar', calendarWritable: true };
        calendars.set(row.calendar_id, calendars.get(row.calendar_id) || { id: row.calendar_id, graphId: '', name: row.event.calendarName || 'Calendar', canEdit: row.calendar_id === DEFAULT_CALENDAR, isDefault: row.calendar_id === DEFAULT_CALENDAR });
        rows.set(rowKey(row.calendar_id, row.event_id), row);
      }
      stateRow = await pb.first('state', `k = ${quote(STATE_KEY)}`);
      progress = stateRow?.v || null;
      for (const cursor of Object.values(progress?.calendars || {})) if (cursor.metadata?.id) calendars.set(cursor.metadata.id, cursor.metadata);
      // Older copies omitted privacy and response state. Re-read affected
      // calendars once: unchanged events will not reappear in an old delta.
      for (const row of rows.values()) if (!row.event.sensitivity || !row.event.responseStatus) {
        if (progress?.calendars) delete progress.calendars[row.calendar_id];
        else progress = null;
      }
      syncedAt = progress?.syncedAt || Math.max(0, ...Object.values(progress?.calendars || {}).map(value => value.syncedAt || 0));
      state = syncedAt ? 'cached' : 'starting';
    },
    sync, status, get,
    list({ start, end }) {
      const low = Date.parse(start), high = Date.parse(end);
      if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low || high - low > 366 * 86400000) throw new GraphError('Choose a valid calendar range of up to one year', 400);
      return [...rows.values()].map(row => row.event).filter(event => event.type !== 'seriesMaster' && overlaps(event, low, high)).sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
    },
    get, create: input => write('POST', '', input), update: (key, input) => write('PATCH', key, input),
    async respond(key, input) {
      if (!input || !['accept', 'decline'].includes(input.response) || Object.keys(input).some(field => field !== 'response'))
        throw new GraphError('Choose accept or decline', 400);
      if (inflight) await inflight.catch(() => {});
      const row = findByLocalKey(key);
      if (!row) throw new GraphError('The cached event was not found', 404);
      const calendarId = row.calendar_id || DEFAULT_CALENDAR, calendar = calendarMeta(calendarId);
      if (!calendar?.canEdit || calendar.accessible === false) throw new GraphError('This calendar is read-only or unavailable', 403);
      if (row.event.isOrganizer || row.event.cancelled || !row.event.organizer?.address)
        throw new GraphError('Only active meeting invitations can be answered', 400);
      const base = calendarId === DEFAULT_CALENDAR ? '/me/events' : `/me/calendars/${encodeURIComponent(calendarId)}/events`;
      await graph.request('POST', `${base}/${encodeURIComponent(row.event_id)}/${input.response}`, { body: { sendResponse: true }, priority: 0 });
      if (input.response === 'decline') { await erase(row.event_id, calendarId); return null; }
      const event = { ...row.event, responseStatus: { response: 'accepted', time: new Date(now()).toISOString() } };
      rows.set(rowKey(calendarId, row.event_id), await pb.update('calendar_events', row.id, { event }));
      return event;
    },
    async remove(key, calendarId = DEFAULT_CALENDAR) { if (inflight) await inflight.catch(() => {});
      const row = findByLocalKey(key);
      if (!row) throw new GraphError('The cached event was not found', 404);
      calendarId = row.calendar_id || DEFAULT_CALENDAR;
      const calendar = calendarMeta(calendarId);
      if (!calendar || !calendar.canEdit || calendar.accessible === false) throw new GraphError('This calendar is read-only or unavailable', 403);
      const base = calendarId === DEFAULT_CALENDAR ? '/me/events' : `/me/calendars/${encodeURIComponent(calendarId)}/events`;
      await graph.request('DELETE', `${base}/${encodeURIComponent(row.event_id)}`, { priority: 0 }); await erase(row.event_id, calendarId); },
    start() { if (!stopped) return; stopped = false; void tick(); },
    stop() { stopped = true; clearTimeout(timer); }
  };
}
