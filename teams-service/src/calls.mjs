import http from 'node:http';
import path from 'node:path';

const MAX_REPLY = 128 * 1024;
const ACTIONS = new Set(['open-meeting', 'open-chat', 'continue-web', 'join', 'answer-audio',
  'answer-video', 'decline', 'call-audio', 'call-video', 'mute', 'unmute', 'camera-on',
  'camera-off', 'hang-up', 'focus']);
const CHAT_ACTIONS = new Set(['open-chat', 'call-audio', 'call-video']);
const FAILURE_MESSAGES = new Map([
  [400, 'The Teams browser refused the action.'],
  [409, 'The Teams call state changed or the action is unavailable. Refresh the call state.'],
  [503, 'The Teams browser connection is unavailable.'],
  [504, 'Teams did not acknowledge the action. Check the call state before trying again.'],
]);

export class CallError extends Error {
  constructor(status, message, code = 'call_unavailable') {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function validateAction(body) {
  const { action, sessionId, revision } = body;
  const fields = ['action', 'sessionId', 'revision'];
  let target;
  if (action === 'open-meeting') {
    fields.push('url');
    target = body.url;
  } else if (CHAT_ACTIONS.has(action)) {
    fields.push('chatId');
    target = body.chatId;
  }
  if (!ACTIONS.has(action) || typeof sessionId !== 'string' || !sessionId || sessionId.length > 256 ||
      !Number.isSafeInteger(revision) || revision < 0 ||
      Object.keys(body).some(field => !fields.includes(field)) ||
      (fields.length === 4 && (typeof target !== 'string' || !target || target.length > 4096))) {
    throw new CallError(400, 'Invalid Teams call action.', 'invalid_action');
  }
}

export function createCalls({ socket, owner }) {
  if (!socket && !owner) return undefined;
  if (!socket || !path.isAbsolute(socket) || !owner) {
    throw new Error('Teams call controls require an owner and an absolute socket path');
  }

  function request(method, route, body) {
    return new Promise((resolve, reject) => {
      const req = http.request({ socketPath: socket, path: route, method,
        headers: body ? { 'Content-Type': 'application/json' } : {} }, response => {
        let size = 0;
        const chunks = [];
        response.on('data', chunk => {
          size += chunk.length;
          if (size > MAX_REPLY) {
            reject(new CallError(502, 'The Teams browser returned an oversized response.'));
            response.destroy();
            return;
          }
          chunks.push(chunk);
        });
        response.on('error', () => reject(new CallError(502, 'The Teams browser response was interrupted.')));
        response.on('end', () => {
          let result;
          try { result = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { /* validated below */ }
          if (!result || typeof result !== 'object' || Array.isArray(result)) {
            reject(new CallError(502, 'The Teams browser returned an invalid response.'));
          } else if (response.statusCode >= 400) {
            const status = FAILURE_MESSAGES.has(response.statusCode) ? response.statusCode : 502;
            const code = typeof result.code === 'string' && /^[a-z0-9_-]{1,80}$/.test(result.code) ? result.code : undefined;
            reject(new CallError(status, FAILURE_MESSAGES.get(status) || 'The Teams browser did not confirm the action.', code));
          } else if (method === 'POST' && result.ok !== true) {
            reject(new CallError(502, 'The Teams browser did not confirm the action.'));
          } else {
            resolve(result);
          }
        });
      });
      const deadline = setTimeout(() => {
        reject(new CallError(504, FAILURE_MESSAGES.get(504)));
        req.destroy();
      }, method === 'POST' ? 12000 : 2000);
      req.once('close', () => clearTimeout(deadline));
      req.on('error', () => reject(new CallError(503, FAILURE_MESSAGES.get(503))));
      req.end(body ? JSON.stringify(body) : undefined);
    });
  }

  return {
    owner,
    state: () => request('GET', '/state'),
    action(body) {
      validateAction(body);
      return request('POST', '/actions', body);
    },
  };
}
