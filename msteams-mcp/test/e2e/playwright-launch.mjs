// Proves patches/browser-firefox.cjs against the real thing: the package's own createBrowserContext()
// starts the Playwright Firefox of the component (headless, a persistent profile under $HOME), loads a
// page and closes it. Run with the environment of malrunar-msteams-mcp (lib/common.sh).
const PKG = process.env.MALRUNAR_MSTEAMS_MCP_PKG;
const { createBrowserContext, closeBrowser } = await import(`${PKG}/browser/context.js`);
const manager = await createBrowserContext({ headless: true });
try {
  await manager.page.goto('data:text/html,<title>patched firefox</title><p id=x>ok</p>');
  const title = await manager.page.title();
  const agent = await manager.page.evaluate(() => navigator.userAgent);
  console.log(JSON.stringify({ title, agent }));
  if (title !== 'patched firefox' || !/Firefox\//.test(agent)) process.exit(1);
} finally {
  await closeBrowser(manager, false);
}
