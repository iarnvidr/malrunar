#!/usr/bin/env python3
"""Patch pinned Tail browser MCP (the checkout given as argv[1]) with the malrunar adapter changes.

usage: prepare-tail.py <tail-checkout> <browser-id>
"""
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
browser_id = sys.argv[2]
if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', browser_id):
    raise SystemExit('browser id must match [A-Za-z0-9_.-]{1,64}')
manifest = root / 'firefox-extension/manifest.json'
data = json.loads(manifest.read_text())
data['version'] = '2.0.23.2'
# The extension must reach its loopback health probe before any site is approved.
data['permissions'].append('http://127.0.0.1/*')
manifest.write_text(json.dumps(data, indent=2) + '\n')


def replace(path, old, new):
    contents = path.read_text()
    if contents.count(old) != 1:
        raise SystemExit(f'Tail source changed: expected one match in {path.name}')
    path.write_text(contents.replace(old, new))


# Machine-readable metadata preserves titles/URLs containing commas or newlines.
replace(root / 'mcp-server/mcp-tools.ts',
        'content: [paginationInfo, ...tabContent],',
        'content: [paginationInfo, ...tabContent],\n'
        '        structuredContent: { tabs: paginatedTabs, total: totalTabs, hasMore },')
replace(root / 'firefox-extension/extension-config.ts',
        'config.browserId = generateBrowserId();',
        f'config.browserId = {json.dumps(browser_id)};')
# Allow the existing browser_navigate tool to use tabs.update, including from
# about:newtab. It needs no page script and still checks the destination deny list.
replace(root / 'common/server-messages.ts',
        'cmd: "open-tab";\n  url: string;',
        'cmd: "open-tab";\n  url: string;\n  tabId?: number;')
replace(root / 'mcp-server/mcp-tools.ts',
        '{ browserId: browserIdSchema, url: z.string() },\n    async ({ browserId, url }) => {',
        '{ browserId: browserIdSchema, url: z.string(), tabId: z.number().optional() },\n'
        '    async ({ browserId, url, tabId }) => {')
replace(root / 'mcp-server/mcp-tools.ts',
        'browserApi.openTab(id, url);', 'browserApi.openTab(id, url, tabId);')
replace(root / 'mcp-server/browser-api.ts',
        'url: string\n  ): Promise<number | undefined>',
        'url: string,\n    tabId?: number\n  ): Promise<number | undefined>')
replace(root / 'mcp-server/browser-api.ts',
        '{ cmd: "open-tab", url },', '{ cmd: "open-tab", url, tabId },')
replace(root / 'firefox-extension/message-handler.ts',
        'this.openUrl(req.correlationId, req.url);',
        'this.openUrl(req.correlationId, req.url, req.tabId);')
replace(root / 'firefox-extension/message-handler.ts',
        'if ("tabId" in req) {',
        'if ("tabId" in req && req.tabId !== undefined) {')
replace(root / 'firefox-extension/message-handler.ts',
        'private async openUrl(correlationId: string, url: string): Promise<void>',
        'private async openUrl(correlationId: string, url: string, tabId?: number): Promise<void>')
replace(root / 'firefox-extension/message-handler.ts',
        'const tab = await browser.tabs.create({\n      url,\n    });',
        'const tab = tabId === undefined\n'
        '      ? await browser.tabs.create({ url })\n'
        '      : await browser.tabs.update(tabId, { url });')
# Keep upstream's optional host permissions and domain approval UI unchanged.
