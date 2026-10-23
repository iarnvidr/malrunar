// Runs in the page before its own scripts, with `cfg` ({ hosts: [...] }) from the app configuration.
// A click on a link to another site, or on a target=_blank link, goes through window.open, which the
// app routes: the site and its named sign-in hosts load in this window, everything else opens in the
// system browser. (Navigations that scripts start by themselves are filtered by the app, not here.)
document.addEventListener('click', (event) => {
  if (event.defaultPrevented || event.button !== 0) return;
  const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
  if (!link) return;
  let url;
  try { url = new URL(link.href, location.href); } catch { return; }
  if (!/^https?:$/.test(url.protocol)) return;
  const host = url.host.toLowerCase();
  const inApp = cfg.hosts.some((pattern) => pattern.startsWith('*.')
    ? host.endsWith(pattern.slice(1)) && host.length > pattern.length - 1
    : host === pattern);
  if (link.target !== '_blank' && inApp) return;
  event.preventDefault();
  window.open(url.href, '_blank');
}, true);
