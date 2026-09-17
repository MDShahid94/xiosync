/**
 * d1.mjs — Cloudflare D1 HTTP client for XIO Mesh (JavaScript).
 *
 * Replaces Supabase REST calls in db.mjs, session-manager.mjs,
 * runtime-manager.mjs and workflow files.
 *
 * Bootstrap: cf_api_token, cf_account_id, cf_d1_database_id are read from
 *   process.env (set from /tmp/xio_config.json by boot.py before node starts).
 *
 * D1 HTTP API:
 *   POST /accounts/{account_id}/d1/database/{db_id}/query
 *   Authorization: Bearer {cf_api_token}
 *   Body: { sql: "...", params: [...] }
 *   Response: { success: bool, result: [{ results: [...], meta: {...} }] }
 */

const D1_BASE = 'https://api.cloudflare.com/client/v4/accounts';

class D1Error extends Error {
  constructor(message, sql) {
    super(message);
    this.name = 'D1Error';
    this.sql = sql;
  }
}

export class D1Client {
  /**
   * @param {object} opts
   * @param {string} opts.accountId
   * @param {string} opts.databaseId
   * @param {string} opts.apiToken
   */
  constructor({ accountId, databaseId, apiToken }) {
    this._url = `${D1_BASE}/${accountId}/d1/database/${databaseId}/query`;
    this._headers = {
      'Authorization': `Bearer ${apiToken}`,
      'Content-Type':  'application/json',
    };
  }

  // ── Low-level ──────────────────────────────────────────────────────────────

  /** Execute SQL and return array of result rows. */
  async query(sql, params = []) {
    const res = await fetch(this._url, {
      method:  'POST',
      headers: this._headers,
      body:    JSON.stringify({ sql, params }),
    });
    const data = await res.json();
    if (!data.success) {
      throw new D1Error(
        `D1 query failed: ${JSON.stringify(data.errors)}`,
        sql
      );
    }
    return data.result[0].results ?? [];
  }

  /** Execute a write statement, returns rows_written count. */
  async execute(sql, params = []) {
    const res = await fetch(this._url, {
      method:  'POST',
      headers: this._headers,
      body:    JSON.stringify({ sql, params }),
    });
    const data = await res.json();
    if (!data.success) {
      throw new D1Error(
        `D1 execute failed: ${JSON.stringify(data.errors)}`,
        sql
      );
    }
    return data.result[0].meta?.rows_written ?? 0;
  }

  // ── High-level helpers (mirror Supabase PostgREST patterns) ───────────────

  /**
   * SELECT with optional WHERE / ORDER BY / LIMIT.
   * @param {string} table
   * @param {object} [opts]
   * @param {string} [opts.where]    - e.g. "email=? AND is_active=?"
   * @param {Array}  [opts.params]   - bound values for WHERE placeholders
   * @param {string} [opts.columns]  - default "*"
   * @param {string} [opts.order]    - e.g. "serial ASC"
   * @param {number} [opts.limit]    - 0 = no limit
   */
  async select(table, { where, params = [], columns = '*', order, limit = 0 } = {}) {
    let sql = `SELECT ${columns} FROM ${table}`;
    if (where)  sql += ` WHERE ${where}`;
    if (order)  sql += ` ORDER BY ${order}`;
    if (limit)  sql += ` LIMIT ${limit}`;
    return this.query(sql, params);
  }

  /** SELECT returning first row or null. */
  async selectOne(table, where, params = [], columns = '*') {
    const rows = await this.select(table, { where, params, columns, limit: 1 });
    return rows[0] ?? null;
  }

  /**
   * INSERT OR REPLACE (matches Supabase 'resolution=merge-duplicates').
   * @param {string} table
   * @param {object} data  - { column: value, ... }
   */
  async upsert(table, data) {
    const cols = Object.keys(data).join(', ');
    const ph   = Object.keys(data).map(() => '?').join(', ');
    return this.execute(
      `INSERT OR REPLACE INTO ${table} (${cols}) VALUES (${ph})`,
      Object.values(data)
    );
  }

  /**
   * UPDATE with WHERE clause.
   * @param {string} table
   * @param {object} data        - { column: newValue, ... }
   * @param {string} where       - e.g. "id=?"
   * @param {Array}  whereParams - bound values for WHERE
   */
  async update(table, data, where, whereParams = []) {
    const sets = Object.keys(data).map(k => `${k}=?`).join(', ');
    return this.execute(
      `UPDATE ${table} SET ${sets} WHERE ${where}`,
      [...Object.values(data), ...whereParams]
    );
  }

  /** DELETE with WHERE clause. */
  async delete(table, where, params = []) {
    return this.execute(`DELETE FROM ${table} WHERE ${where}`, params);
  }

  /** COUNT(*) with optional WHERE. */
  async count(table, where = '', params = []) {
    const sql = `SELECT COUNT(*) AS n FROM ${table}` + (where ? ` WHERE ${where}` : '');
    const rows = await this.query(sql, params);
    return rows[0]?.n ?? 0;
  }

  // ── Lock helpers ───────────────────────────────────────────────────────────

  /**
   * Atomically acquire a TTL lock. Returns true if acquired, false if held.
   * Key namespaces: 'session:{id}', 'ts:{node_name}', 'cache:{name}'
   */
  async acquireLock(key, node, ttlSeconds = 120) {
    const now = Date.now() / 1000;
    const written = await this.execute(
      `INSERT OR IGNORE INTO locks (key, node, acquired_at, expires_at)
       SELECT ?, ?, ?, ?
       WHERE NOT EXISTS (
         SELECT 1 FROM locks WHERE key=? AND expires_at > ?
       )`,
      [key, node, now, now + ttlSeconds, key, now]
    );
    return written > 0;
  }

  async releaseLock(key, node) {
    return (await this.execute(
      'DELETE FROM locks WHERE key=? AND node=?', [key, node]
    )) > 0;
  }

  async isLocked(key) {
    const now = Date.now() / 1000;
    const rows = await this.query(
      'SELECT 1 FROM locks WHERE key=? AND expires_at > ?', [key, now]
    );
    return rows.length > 0;
  }

  // ── Secrets helper ─────────────────────────────────────────────────────────

  async getSecret(key) {
    const row = await this.selectOne('node_secrets', 'key=?', [key], 'value');
    return row?.value ?? null;
  }

  // ── Runtime config helper ──────────────────────────────────────────────────

  /**
   * Read runtime_config for a node (per-node values override global).
   * Returns merged { key: value } object.
   */
  async getConfig(nodeName = 'global') {
    const rows = await this.query(
      `SELECT key, value, scope FROM runtime_config
       WHERE scope IN ('global', ?)
       ORDER BY CASE WHEN scope=? THEN 0 ELSE 1 END`,
      [nodeName, nodeName]
    );
    // Process global first (index 1), then node-specific (index 0) overwrites
    const merged = {};
    for (const r of [...rows].reverse()) merged[r.key] = r.value;
    return merged;
  }

  async setConfig(key, value, scope = 'global') {
    return this.upsert('runtime_config', {
      key, value: String(value), scope,
      updated_at: new Date().toISOString(),
    });
  }
}

// ── Module-level singleton ──────────────────────────────────────────────────

let _client = null;

/**
 * Returns a cached D1Client built from environment variables:
 *   CF_API_TOKEN, CF_ACCOUNT_ID, CF_D1_DATABASE_ID
 * These are injected by boot.py from /tmp/xio_config.json before node starts.
 */
export function getD1Client() {
  if (_client) return _client;
  const token  = process.env.CF_API_TOKEN;
  const acct   = process.env.CF_ACCOUNT_ID;
  const dbId   = process.env.CF_D1_DATABASE_ID;
  if (!token || !acct || !dbId) {
    throw new D1Error(
      'D1 credentials missing. Ensure CF_API_TOKEN, CF_ACCOUNT_ID, ' +
      'CF_D1_DATABASE_ID are set in environment (boot.py injects from xio_config.json).'
    );
  }
  _client = new D1Client({ accountId: acct, databaseId: dbId, apiToken: token });
  return _client;
}

export function resetD1Client() {
  _client = null;
}

export { D1Error };
