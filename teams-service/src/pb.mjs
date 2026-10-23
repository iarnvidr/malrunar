// Minimal PocketBase REST client (superuser), loopback only. No dependencies.
export class PocketBaseError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'PocketBaseError';
    this.status = status;
    this.body = body;
  }
}

// PocketBase filter string literal.
export const quote = value => `'${String(value).replaceAll('\\', '\\\\').replaceAll("'", "\\'")}'`;

export class PocketBase {
  constructor({ url, identity, password, fetchImpl = fetch }) {
    this.url = url.replace(/\/$/, '');
    this.identity = identity;
    this.password = password;
    this.fetch = fetchImpl;
    this.token = null;
    this.authenticating = null;
  }

  async authenticate() {
    this.authenticating ??= (async () => {
      const response = await this.fetch(`${this.url}/api/collections/_superusers/auth-with-password`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity: this.identity, password: this.password })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new PocketBaseError('PocketBase refused the service login', response.status, body);
      this.token = body.token;
    })().finally(() => { this.authenticating = null; });
    return this.authenticating;
  }

  async request(method, path, { query, body } = {}) {
    if (!this.token) await this.authenticate();
    const url = new URL(`${this.url}${path}`);
    for (const [key, value] of Object.entries(query || {})) if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await this.fetch(url, {
        method, headers: { Authorization: this.token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined
      });
      if ((response.status === 401 || response.status === 403) && attempt === 0) { await this.authenticate(); continue; }
      const text = await response.text();
      let payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
      if (!response.ok) throw new PocketBaseError(`PocketBase ${method} ${path}: ${response.status}`, response.status, payload);
      return payload;
    }
    throw new PocketBaseError('PocketBase request failed', 0);
  }

  async list(collection, { filter, sort, perPage = 200, page = 1, fields, skipTotal = true } = {}) {
    const body = await this.request('GET', `/api/collections/${collection}/records`, {
      query: { filter, sort, perPage, page, fields, skipTotal: skipTotal ? 1 : undefined }
    });
    return body.items;
  }

  async listAll(collection, options = {}) {
    const perPage = options.perPage || 500;
    const items = [];
    for (let page = 1; ; page++) {
      const batch = await this.list(collection, { ...options, perPage, page });
      items.push(...batch);
      if (batch.length < perPage) return items;
    }
  }

  async first(collection, filter, options = {}) {
    return (await this.list(collection, { ...options, filter, perPage: 1 }))[0] || null;
  }

  async count(collection, filter) {
    const body = await this.request('GET', `/api/collections/${collection}/records`, { query: { filter, perPage: 1, fields: 'id' } });
    return body.totalItems;
  }

  create(collection, data) { return this.request('POST', `/api/collections/${collection}/records`, { body: data }); }
  update(collection, id, data) { return this.request('PATCH', `/api/collections/${collection}/records/${id}`, { body: data }); }
  remove(collection, id) { return this.request('DELETE', `/api/collections/${collection}/records/${id}`); }
}
