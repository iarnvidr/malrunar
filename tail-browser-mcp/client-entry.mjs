// Bundled with Tail's MCP SDK at image build time.
// Environment: TAIL_MCP_URL (default http://127.0.0.1:18790/mcp), TAIL_BROWSER_ID (default: the id baked
// into the extension at build time, `browser` unless the TAIL_BROWSER_ID build arg was changed).
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

export async function connection(fn) {
  const client = new Client({ name: 'tail-browser-client', version: '2.0.0' }, { capabilities: {} });
  const transport = new StreamableHTTPClientTransport(new URL(
    process.env.TAIL_MCP_URL || 'http://127.0.0.1:18790/mcp'));
  const tool = async (name, args = {}) => {
    const result = await client.callTool({ name, arguments: { browserId: process.env.TAIL_BROWSER_ID || __TAIL_DEFAULT_BROWSER_ID__, ...args } });
    if (result.isError || result.content?.some(item => item.isError)) {
      throw new Error(result.content?.map(item => item.text || '').join('\n') || `${name} failed`);
    }
    return result;
  };
  const tabs = async () => {
    const result = await tool('get-list-of-open-tabs', { limit: 500 });
    if (!Array.isArray(result.structuredContent?.tabs)) throw new Error('Tail returned no tab metadata');
    if (result.structuredContent.hasMore) throw new Error('More than 500 browser tabs are open');
    return result.structuredContent.tabs;
  };
  const evaluate = async (context, expression, args) => {
    const tab = (await tabs()).find(tab => String(tab.id) === context);
    if (!tab) throw new Error('The browser tab closed before it could be read');
    if (tab.discarded) {
      // Stock Firefox restores background tabs lazily. Load only the requested
      // tab, without activating it, before asking its content process for text.
      await tool('open-browser-tab', { tabId: tab.id, url: tab.url });
      const deadline = Date.now() + 60000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        const ready = (await tabs()).find(item => item.id === tab.id);
        if (!ready) throw new Error('The browser tab closed while loading');
        if (!ready.discarded && ready.status === 'complete') break;
        if (Date.now() >= deadline) throw new Error('Background tab loading timed out');
      }
    }
    const result = await tool('evaluate-script-in-tab', {
      tabId: Number(context), function: expression, args,
    });
    return JSON.parse(result.content?.find(item => item.type === 'text')?.text);
  };
  const send = async (method, params = {}) => {
    if (method === 'browsingContext.getTree') return { contexts: (await tabs()).map(tab => ({
      context: String(tab.id), url: tab.url, title: tab.title, active: tab.active, status: tab.status, discarded: tab.discarded,
    })) };
    if (method === 'script.evaluate') return { type: 'success', result: {
      value: await evaluate(params.target.context, `async () => (${params.expression})`),
    } };
    if (method === 'browsingContext.navigate') {
      await tool('open-browser-tab', { tabId: Number(params.context), url: params.url });
      const deadline = Date.now() + 60000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        const tab = (await tabs()).find(tab => String(tab.id) === params.context);
        if (!tab) throw new Error('The browser tab closed during navigation');
        if (tab.status === 'complete') return {};
      }
      throw new Error('Browser navigation timed out');
    }
    if (method === 'browsingContext.close') {
      await tool('close-browser-tabs', { tabIds: [Number(params.context)] });
      return {};
    }
    throw new Error(`Unsupported extension operation: ${method}`);
  };
  try {
    await client.connect(transport);
    return await fn(send);
  } finally {
    await transport.terminateSession().catch(() => {});
    await client.close().catch(() => {});
  }
}
