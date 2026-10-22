// Runs in the page before its own scripts (`cfg` is not used here).
// A desktop web view has no Notification permission prompt and no push; this gives the page the
// standard `Notification` constructor and `navigator.setAppBadge`, and hands what it asks for to the
// app, which shows a native notification or sets the Dock badge. Notifications cannot be shown while
// the app is not running, and are not queued (see README).
const invoke = (command, args) => window.__TAURI_INTERNALS__.invoke(command, args);
const live = new Map();
const LIVE_MAX = 200;
let counter = 0;

class WrapperNotification extends EventTarget {
  constructor(title, options = {}) {
    super();
    if (arguments.length === 0) throw new TypeError("Failed to construct 'Notification': 1 argument required, but only 0 present.");
    const opts = options && typeof options === 'object' ? options : {};
    Object.defineProperties(this, {
      title: { value: String(title), enumerable: true },
      body: { value: opts.body == null ? '' : String(opts.body), enumerable: true },
      tag: { value: opts.tag == null ? '' : String(opts.tag), enumerable: true },
      data: { value: opts.data === undefined ? null : opts.data, enumerable: true },
      silent: { value: opts.silent === undefined ? null : opts.silent, enumerable: true },
    });
    this.onclick = null;
    this.onshow = null;
    this.onerror = null;
    this.onclose = null;
    const id = String(++counter);
    live.set(id, this);
    if (live.size > LIVE_MAX) live.delete(live.keys().next().value);
    Object.defineProperty(this, '__id', { value: id });
    invoke('wrapper_notify', { id, title: this.title, body: this.body }).then(
      () => this.dispatch('show'),
      () => this.dispatch('error'),
    );
  }

  dispatch(type) {
    const event = new Event(type);
    const handler = this['on' + type];
    if (typeof handler === 'function') {
      try { handler.call(this, event); } catch (error) { setTimeout(() => { throw error; }); }
    }
    this.dispatchEvent(event);
  }

  close() {
    live.delete(this.__id);
    this.dispatch('close');
  }

  static get permission() { return 'granted'; }
  static get maxActions() { return 0; }
  static requestPermission(callback) {
    if (typeof callback === 'function') callback('granted');
    return Promise.resolve('granted');
  }
}

// Called by the app when the user clicks a notification.
Object.defineProperty(window, '__wrapperNotificationClick', {
  value(id) {
    const notification = live.get(String(id));
    if (notification) notification.dispatch('click');
    window.focus();
  },
});

Object.defineProperty(window, 'Notification', { value: WrapperNotification, writable: true, configurable: true });

// A count shows as the number, no argument as a dot, zero (or anything else) clears the badge.
const badge = (label) => invoke('wrapper_badge', { label }).catch(() => {});
Object.defineProperty(navigator, 'setAppBadge', {
  value(count) {
    const number = Math.floor(Number(count));
    return badge(count === undefined ? '\u2022' : number > 0 ? String(Math.min(number, 99999)) : null);
  },
  configurable: true,
});
Object.defineProperty(navigator, 'clearAppBadge', { value() { return badge(null); }, configurable: true });
