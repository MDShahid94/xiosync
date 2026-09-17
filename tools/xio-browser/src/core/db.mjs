import Database from 'better-sqlite3';
import { createLogger } from '../utils/logger.mjs';
import { execSync as _execSync } from 'node:child_process';
import { getD1Client, D1Error } from './d1.mjs';

const log = createLogger('db');
let _db  = null;
let _d1  = null;   // D1Client singleton (set in initDb)

/**
 * Thin adapter matching the old _supa interface so call-sites need no changes.
 * All remote writes now go to Cloudflare D1 instead of Supabase REST.
 *
 * Table/column renames vs old Supabase schema:
 *   session_services  → session_credentials
 *   session_valid     → is_valid
 *   service_sessions  → session_credentials (merged)
 */
const _supa = {
  // select() returns rows — used only in initDb for bulk load
  async select(table) {
    if (!_d1) return [];
    // Map old Supabase table names → D1 table names
    const d1Table = _tableMap(table);
    try {
      return await _d1.select(d1Table, { order: 'rowid ASC' });
    } catch (e) { log.warn(`D1 SELECT ${d1Table} failed: ${e.message}`); return []; }
  },

  async upsert(table, body) {
    if (!_d1) return;
    const d1Table = _tableMap(table);
    const row = _mapRow(table, Array.isArray(body) ? body[0] : body);
    try { await _d1.upsert(d1Table, row); }
    catch (e) { log.warn(`D1 UPSERT ${d1Table} failed: ${e.message}`); }
  },

  async update(table, patch, eq) {
    if (!_d1) return;
    const d1Table  = _tableMap(table);
    const d1Patch  = _mapRow(table, patch);
    const [col, val] = eq;
    const d1Col    = _colMap(table, col);
    try { await _d1.update(d1Table, d1Patch, `${d1Col}=?`, [val]); }
    catch (e) { log.warn(`D1 UPDATE ${d1Table} failed: ${e.message}`); }
  },

  async delete(table, eq) {
    if (!_d1) return;
    const d1Table  = _tableMap(table);
    const [col, val] = eq;
    const d1Col    = _colMap(table, col);
    try { await _d1.delete(d1Table, `${d1Col}=?`, [val]); }
    catch (e) { log.warn(`D1 DELETE ${d1Table} failed: ${e.message}`); }
  },
};

// ── Table/column name mapping (Supabase → D1) ─────────────────────────────
const TABLE_MAP = {
  'accounts':         'accounts',
  'sessions':         'sessions',
  'session_services': 'session_credentials',
  'service_sessions': 'session_credentials',
};
function _tableMap(t) { return TABLE_MAP[t] ?? t; }

// Column renames per table
const COL_MAP = {
  // sessions: drop 'tier' (no such column in D1 — use accounts.tier via JOIN)
  //           rename 'bound_at' → 'exit_node_set_at' (D1 actual column name)
  'sessions': { 'tier': null, 'bound_at': 'exit_node_set_at' },
  'session_services': { 'session_valid': 'is_valid' },
  'service_sessions': { 'state_valid': 'is_valid', 'last_verified': 'last_checked', 'storage_path': null },
};
function _colMap(table, col) {
  return COL_MAP[table]?.[col] ?? col;
}

/** Remap a row object's keys for the destination D1 table. */
function _mapRow(srcTable, row) {
  if (!row || typeof row !== 'object') return row;
  const map = COL_MAP[srcTable] ?? {};
  const out = {};
  for (const [k, v] of Object.entries(row)) {
    const d1k = map[k] ?? k;
    if (d1k === null) continue;   // null = drop this column
    out[d1k] = v;
  }
  return out;
}

// ── In-Memory Maps for Supabase Data ───────────────────────────────────────
const _accounts = new Map();
const _sessions = new Map();
const _sessionServices = new Map(); // session_id -> Map(service -> object)

const SCHEMA = `
CREATE TABLE IF NOT EXISTS accounts (
  email        TEXT PRIMARY KEY,
  tier         TEXT NOT NULL,
  password     TEXT NOT NULL,
  totp_secret  TEXT
);

-- Identity slot: one Chrome profile that holds cookies for any number of domains.
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,    -- e.g. "acc50", "karmareturnsfromallsides"
  serial       INTEGER UNIQUE,      -- stable auto-assigned serial (PRFL-NNN). Never changes.
  display_name TEXT,                -- human-readable label
  notes        TEXT,                -- optional free-text notes
  tier         TEXT NOT NULL DEFAULT 'Starter',
  exit_node    TEXT,                -- Tailscale IP this session is BOUND to (set on first use)
  bound_at     TEXT,                -- datetime when exit_node binding was created
  is_persisted INTEGER DEFAULT 0,   -- 1 if profile/session exist on drive
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tracks which services are logged in within each identity slot.
CREATE TABLE IF NOT EXISTS session_services (
  session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  service       TEXT NOT NULL,       -- e.g. "google", "v0", "github"
  account_hint  TEXT,                -- e.g. "chaina@gmail.com"
  session_valid INTEGER DEFAULT 0,
  last_checked  TEXT,
  PRIMARY KEY (session_id, service)
);

-- Async job records
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  workflow_id   TEXT NOT NULL,
  session_id    TEXT,
  exit_node     TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  started_at    TEXT,
  completed_at  TEXT,
  result        TEXT,
  error         TEXT
);

-- Per-step records within a job
CREATE TABLE IF NOT EXISTS job_steps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL REFERENCES jobs(id),
  step_index  INTEGER NOT NULL,
  step_name   TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'running',
  screenshot  TEXT,
  log_lines   TEXT,
  duration_ms INTEGER,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Engine Success Registry (ESR) — UCB1 adaptive engine selection
-- Key: workflow_id + domain → which stealth engine (camoufox/uc/nodriver/patchright) works best
CREATE TABLE IF NOT EXISTS engine_stats (
  workflow_id   TEXT NOT NULL,
  domain        TEXT NOT NULL DEFAULT 'any',
  engine        TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  successes     INTEGER NOT NULL DEFAULT 0,
  last_win_ts   INTEGER,          -- unix epoch of last success
  last_try_ts   INTEGER,          -- unix epoch of last attempt
  PRIMARY KEY (workflow_id, domain, engine)
);

-- Drive Asset Catalogue — replaces manifest.json
-- Maps every reusable entity to its Drive file/folder ID.
-- Eliminates name-search API calls after first access (ID-first pattern).
--
-- entity_type : 'session'|'profile'|'tailscale'|'job'|'registry'|'workflow'|'db'|'cache'
-- entity_id   : session_id, job_id, node_name, workflow_id, cache_key, ...
-- asset_type  : 'state_json'|'profile_tarball'|'state_file'|'folder'|'file'|'tarball'
-- canonical_name : filename as it sits on Drive (from naming.py output)
CREATE TABLE IF NOT EXISTS drive_assets (
  entity_type      TEXT NOT NULL,
  entity_id        TEXT NOT NULL,
  asset_type       TEXT NOT NULL,
  drive_file_id    TEXT,           -- Drive file ID (for downloadable files)
  drive_folder_id  TEXT,           -- Drive folder ID (for job folders)
  canonical_name   TEXT,           -- canonical filename on Drive
  last_synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (entity_type, entity_id, asset_type)
);

-- Universal service session registry
-- Tracks stored credentials/state for any authenticated service per profile slot.
-- session_id: PRFL slug (e.g. 'sunmontueswednesthursfrisatur7') or 'mesh-admin'
-- service: service identifier (e.g. 'tailscale', 'v0', 'github', 'google')
-- account_hint: username/email used for this service login
-- state_valid: 1 if last verification passed
-- last_verified: ISO timestamp of last validity check
-- last_saved_at: ISO timestamp of last successful state save
-- node_name: which Colab node performed the last save
-- storage_path: Drive path, R2 key, or local path where state is stored
-- metadata: arbitrary JSON for service-specific extras (e.g. {"file_size":12345})
CREATE TABLE IF NOT EXISTS service_sessions (
  session_id    TEXT NOT NULL,
  service       TEXT NOT NULL,
  account_hint  TEXT,
  state_valid   INTEGER DEFAULT 0,
  last_verified TEXT,
  last_saved_at TEXT,
  node_name     TEXT,
  storage_path  TEXT,
  metadata      TEXT,
  PRIMARY KEY (session_id, service)
);

-- Tailscale nodes states registry
CREATE TABLE IF NOT EXISTS node_ts_states (
  node_name   TEXT PRIMARY KEY,
  state_b64   TEXT,
  state_size  INTEGER,
  ts_ip       TEXT,
  session_id  TEXT,
  runtime_id  TEXT,
  updated_at  TEXT DEFAULT (datetime('now'))
);

-- Session locks for distributed mutual exclusion
CREATE TABLE IF NOT EXISTS session_locks (
  session_id   TEXT PRIMARY KEY,
  node_name    TEXT,
  acquired_at  TEXT DEFAULT (datetime('now')),
  expires_at   TEXT
);
`;

export async function initDb(dbPath) {
  if (_db) {
    log.info('DB already open — reusing existing connection');
    return _db;
  }
  log.info(`Opening DB: ${dbPath}`);
  // Defensive: if something (e.g. ensureAllDirs) mistakenly created a directory
  // at the DB path, remove it so SQLite can create the actual file.
  try {
    const { statSync, rmdirSync } = await import('node:fs');
    const st = statSync(dbPath, { throwIfNoEntry: false });
    if (st && st.isDirectory()) {
      log.warn(`DB path is a directory (EISDIR) — removing and recreating as file: ${dbPath}`);
      rmdirSync(dbPath);
    }
  } catch { /* non-fatal — let SQLite report any real error */ }
  _db = new Database(dbPath);
  _db.pragma('journal_mode = WAL');
  _db.pragma('foreign_keys = OFF');   // disable FK enforcement — integrity managed in app layer
  _db.exec(SCHEMA);

  // ── Migrations: add columns/tables if missing (old DB compat) ─────────────────────
  const sessionCols = _db.prepare('PRAGMA table_info(sessions)').all().map(c => c.name);

  if (!sessionCols.includes('exit_node')) {
    _db.exec('ALTER TABLE sessions ADD COLUMN exit_node TEXT');
    log.info('Migration: added sessions.exit_node');
  }
  if (!sessionCols.includes('bound_at')) {
    _db.exec('ALTER TABLE sessions ADD COLUMN bound_at TEXT');
    log.info('Migration: added sessions.bound_at');
  }
  if (!sessionCols.includes('is_persisted')) {
    _db.exec('ALTER TABLE sessions ADD COLUMN is_persisted INTEGER DEFAULT 0');
    log.info('Migration: added sessions.is_persisted');
  }
  if (!sessionCols.includes('serial')) {
    _db.exec('ALTER TABLE sessions ADD COLUMN serial INTEGER');
    // Back-fill serials for existing rows ordered by created_at
    _db.exec(`
      UPDATE sessions SET serial = (
        SELECT COUNT(*) FROM sessions s2
        WHERE s2.created_at <= sessions.created_at
          AND s2.id <= sessions.id
      ) WHERE serial IS NULL
    `);
    log.info('Migration: added sessions.serial (back-filled)');
  }
  // Purge any legacy email-alias rows (id LIKE '%@%') — credentials now live in accounts table.
  const emailCount = _db.prepare("SELECT COUNT(*) AS n FROM sessions WHERE id LIKE '%@%'").get()?.n ?? 0;
  if (emailCount > 0) {
    _db.prepare("DELETE FROM sessions WHERE id LIKE '%@%'").run();
    log.info(`Migration: removed ${emailCount} legacy email-alias session row(s)`);
  }

  // drive_assets table — created by SCHEMA above; confirm it exists
  const tables = _db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => r.name);
  if (!tables.includes('drive_assets')) {
    _db.exec(`
      CREATE TABLE drive_assets (
        entity_type      TEXT NOT NULL,
        entity_id        TEXT NOT NULL,
        asset_type       TEXT NOT NULL,
        drive_file_id    TEXT,
        drive_folder_id  TEXT,
        canonical_name   TEXT,
        last_synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (entity_type, entity_id, asset_type)
      )
    `);
    log.info('Migration: created drive_assets table');
  }

  // node_ts_states table
  if (!tables.includes('node_ts_states')) {
    _db.exec(`
      CREATE TABLE node_ts_states (
        node_name   TEXT PRIMARY KEY,
        state_b64   TEXT,
        state_size  INTEGER,
        ts_ip       TEXT,
        session_id  TEXT,
        runtime_id  TEXT,
        updated_at  TEXT DEFAULT (datetime('now'))
      )
    `);
    log.info('Migration: created node_ts_states table');
  }

  // service_sessions table
  if (!tables.includes('service_sessions')) {
    _db.exec(`
      CREATE TABLE service_sessions (
        session_id    TEXT NOT NULL,
        service       TEXT NOT NULL,
        account_hint  TEXT,
        state_valid   INTEGER DEFAULT 0,
        last_verified TEXT,
        last_saved_at TEXT,
        node_name     TEXT,
        storage_path  TEXT,
        metadata      TEXT,
        PRIMARY KEY (session_id, service)
      )
    `);
    log.info('Migration: created service_sessions table');
  }

  // session_services: add failure_count and checked_by if missing
  const svcCols = _db.prepare('PRAGMA table_info(session_services)').all().map(c => c.name);
  if (!svcCols.includes('failure_count')) {
    _db.exec('ALTER TABLE session_services ADD COLUMN failure_count INTEGER DEFAULT 0');
    log.info('Migration: added session_services.failure_count');
  }
  if (!svcCols.includes('checked_by')) {
    _db.exec('ALTER TABLE session_services ADD COLUMN checked_by TEXT');
    log.info('Migration: added session_services.checked_by');
  }

  // sessions: add last_seen_at if missing
  if (!sessionCols.includes('last_seen_at')) {
    _db.exec('ALTER TABLE sessions ADD COLUMN last_seen_at TEXT');
    log.info('Migration: added sessions.last_seen_at');
  }

  // ── Init D1 & Load into Memory ───────────────────────────────────────────
  try {
    _d1 = getD1Client();
    log.info('Connecting to Cloudflare D1...');

    const [accountsData, sessionsData, credsData] = await Promise.all([
      _supa.select('accounts'),
      _supa.select('sessions'),
      _supa.select('session_credentials'),  // merged session_services + service_sessions
    ]);

    if (accountsData?.length) accountsData.forEach(a => _accounts.set(a.email, a));
    if (sessionsData?.length)  sessionsData.forEach(s => _sessions.set(s.id, s));

    // session_credentials → in-memory _sessionServices map
    // Map D1 column is_valid → session_valid for backward compat with callers
    if (credsData?.length) {
      credsData.forEach(r => {
        const s = { ...r, session_valid: r.is_valid };  // expose both names
        if (!_sessionServices.has(r.session_id)) _sessionServices.set(r.session_id, new Map());
        _sessionServices.get(r.session_id).set(r.service, s);
      });
    }

    log.info(`Loaded D1: ${_accounts.size} accounts, ${_sessions.size} sessions, ${credsData?.length ?? 0} credentials`);

    // ── Mirror D1 sessions → local SQLite (serial preservation) ──────────
    // sync.py reads serials from SQLite (SELECT serial FROM sessions).
    // Without this mirror step, SQLite is empty and _db_assign_serial()
    // assigns sequential 1,2,3... on every fresh runtime, causing
    // PRFL-NNN serial drift.
    if (sessionsData?.length) {
      const upsertStmt = _db.prepare(`
        INSERT OR REPLACE INTO sessions
          (id, serial, tier, is_persisted, exit_node, display_name, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
          (SELECT created_at FROM sessions WHERE id = ?),
          datetime('now')
        ))
      `);
      const mirrorAll = _db.transaction(rows => {
        for (const s of rows) {
          // Derive tier from accounts map (D1 sessions no longer have tier column)
          const acct = _accounts.get(s.account_email);
          upsertStmt.run(
            s.id,
            s.serial,
            acct?.tier     ?? 'Starter',
            s.is_persisted ? 1 : 0,
            s.exit_node    ?? null,
            s.account_email ?? null,   // display_name = account_email
            s.notes        ?? null,
            s.id,
          );
        }
      });
      mirrorAll(sessionsData);
      log.info(`Mirrored ${sessionsData.length} D1 sessions → local SQLite (serials preserved)`);
    }
  } catch (err) {
    log.warn(`D1 init failed (continuing offline): ${err.message}`);
  }

  log.info('DB ready');
  return _db;
}

export function getDb() {
  if (!_db) throw new Error('DB not initialized — call initDb() first');
  return _db;
}

// ── Account helpers ───────────────────────────────────────────────────────

export function listAccounts() {
  return Array.from(_accounts.values());
}

export function getAccount(email) {
  return _accounts.get(email) || null;
}

export function upsertAccount({ email, tier, password, totp_secret, notes = null, is_active = true }) {
  const acc = { email, tier, password, totp_secret, notes, is_active, created_at: new Date().toISOString() };
  _accounts.set(email, { ...(_accounts.get(email) ?? {}), ...acc });
  _supa.upsert('accounts', acc);
}

// ── Session (identity slot) helpers ───────────────────────────────────────

export function upsertSession({ id, display_name, notes, exit_node, tier, account_email }) {
  const slug = id.includes('@') ? id.split('@')[0] : id;
  let s = _sessions.get(slug);

  if (!s) {
    let max = 0;
    for (const existing of _sessions.values()) {
      if (existing.serial > max) max = existing.serial;
    }
    const serial = max + 1;
    // Derive account_email from slug if not provided
    const acctEmail = account_email ?? (display_name?.includes('@') ? display_name : `${slug}@gmail.com`);
    s = {
      id: slug,
      serial,
      display_name: display_name ?? null,
      notes: notes ?? null,
      tier: tier ?? 'Starter',
      exit_node: exit_node ?? null,
      bound_at: exit_node ? new Date().toISOString() : null,
      is_persisted: false,
      account_email: _accounts.has(acctEmail) ? acctEmail : null,
      last_seen_at: null,
      created_at: new Date().toISOString()
    };
  } else {
    if (display_name !== undefined && display_name !== null) s.display_name = display_name;
    if (notes !== undefined && notes !== null) s.notes = notes;
    if (tier !== undefined && tier !== null && tier !== 'Starter') s.tier = tier;
    if (account_email !== undefined && account_email !== null) s.account_email = account_email;
    // exit_node, serial, created_at intentionally not updated on conflict
  }

  _sessions.set(slug, s);
  _supa.upsert('sessions', s);
}

export function getSession(id) {
  // Normalize: if called with email, extract slug
  const slug = id.includes('@') ? id.split('@')[0] : id;
  const session = _sessions.get(slug);
  if (!session) return null;
  // Derive tier from accounts map — D1 sessions no longer have tier column
  const acct = session.account_email ? _accounts.get(session.account_email) : null;
  const tier = acct?.tier ?? session.tier ?? 'Starter';
  return { ...session, tier, services: listSessionServices(slug) };
}

export function listSessions() {
  const sessions = Array.from(_sessions.values()).filter(s => !s.id.includes('@'));
  sessions.sort((a, b) => (a.serial ?? 0) - (b.serial ?? 0));
  return sessions.map(s => {
    const acct = s.account_email ? _accounts.get(s.account_email) : null;
    const tier = acct?.tier ?? s.tier ?? 'Starter';
    return { ...s, tier, services: listSessionServices(s.id) };
  });
}

export function deleteSession(id) {
  _sessionServices.delete(id);
  _sessions.delete(id);
  _supa.delete('session_services', ['session_id', id]);
  _supa.delete('sessions', ['id', id]);
}

export function getSessionExitBinding(id) {
  const row = _sessions.get(id);
  return row ? { exit_node: row.exit_node ?? null, bound_at: row.bound_at ?? null } : null;
}

export function bindSessionToExitNode(sessionId, exitNodeIP) {
  const s = _sessions.get(sessionId);
  if (s && s.exit_node == null) {
    s.exit_node = exitNodeIP;
    s.bound_at = new Date().toISOString();
    // Only patch if exit_node still null in Supabase (race-safe)
    _supa.update('sessions', { exit_node: s.exit_node, bound_at: s.bound_at }, ['id', sessionId]);
  }
}

export function setSessionPersisted(sessionId, value) {
  const s = _sessions.get(sessionId);
  if (s) {
    s.is_persisted = Boolean(value);
    _supa.update('sessions', { is_persisted: s.is_persisted }, ['id', sessionId]);
  }
}

/**
 * Update last_seen_at for a session to NOW.
 * Called by job-manager when a workflow starts for this session.
 */
export function touchSession(sessionId) {
  const slug = sessionId.includes('@') ? sessionId.split('@')[0] : sessionId;
  const s = _sessions.get(slug);
  if (s) {
    s.last_seen_at = new Date().toISOString();
    _supa.update('sessions', { last_seen_at: s.last_seen_at }, ['id', slug]);
  }
}

// ── session_services helpers ───────────────────────────────────────────────

export function listSessionServices(sessionId) {
  const map = _sessionServices.get(sessionId);
  if (!map) return [];
  const arr = Array.from(map.values());
  arr.sort((a, b) => a.service.localeCompare(b.service));
  return arr;
}

export function upsertSessionService({ session_id, service, account_hint, session_valid, checked_by }) {
  if (!_sessionServices.has(session_id)) _sessionServices.set(session_id, new Map());
  const map = _sessionServices.get(session_id);
  const existing = map.get(service);
  const isValid = Boolean(session_valid);
  const now = new Date().toISOString();

  let obj;
  if (existing) {
    obj = { 
      ...existing, 
      account_hint: account_hint !== undefined ? account_hint : existing.account_hint, 
      session_valid: isValid,
      // reset failure_count on success, increment on failure
      failure_count: isValid ? 0 : (existing.failure_count ?? 0) + 1,
      checked_by: checked_by ?? existing.checked_by ?? null,
      last_checked: now 
    };
  } else {
    obj = { session_id, service, account_hint: account_hint ?? null, session_valid: isValid,
             failure_count: isValid ? 0 : 1, checked_by: checked_by ?? null, last_checked: now };
  }
  
  map.set(service, obj);
  _supa.upsert('session_services', obj);
}

export function markServiceValid(sessionId, service, valid, checkedBy) {
  if (!_sessionServices.has(sessionId)) _sessionServices.set(sessionId, new Map());
  const map = _sessionServices.get(sessionId);
  const existing = map.get(service);
  const isValid = Boolean(valid);
  const now = new Date().toISOString();

  let obj;
  if (existing) {
    obj = { ...existing, session_valid: isValid,
             failure_count: isValid ? 0 : (existing.failure_count ?? 0) + 1,
             checked_by: checkedBy ?? existing.checked_by ?? null,
             last_checked: now };
  } else {
    obj = { session_id: sessionId, service, session_valid: isValid,
             failure_count: isValid ? 0 : 1, checked_by: checkedBy ?? null,
             account_hint: null, last_checked: now };
  }
  
  map.set(service, obj);
  _supa.upsert('session_services', obj);
}

// ── Job helpers (Local SQLite) ─────────────────────────────────────────────

export function createJob(job) {
  getDb().prepare(`
    INSERT INTO jobs (id, workflow_id, session_id, exit_node, status)
    VALUES (@id, @workflow_id, @session_id, @exit_node, @status)
  `).run(job);
}

export function getJob(id) {
  return getDb().prepare('SELECT * FROM jobs WHERE id = ?').get(id);
}

export function listJobs({ limit = 20, status } = {}) {
  if (status) {
    return getDb().prepare(
      'SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?'
    ).all(status, limit);
  }
  return getDb().prepare('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?').all(limit);
}

export function updateJobStatus(id, status, result = null, error = null) {
  getDb().prepare(`
    UPDATE jobs SET
      status = ?,
      started_at   = CASE WHEN ? = 'running' AND started_at IS NULL THEN datetime('now') ELSE started_at END,
      completed_at = CASE WHEN ? IN ('done','error','cancelled') THEN datetime('now') ELSE completed_at END,
      result = ?,
      error  = ?
    WHERE id = ?
  `).run(status, status, status, result ? JSON.stringify(result) : null, error, id);
}

// ── Step helpers (Local SQLite) ────────────────────────────────────────────

export function insertStep(step) {
  getDb().prepare(`
    INSERT INTO job_steps (job_id, step_index, step_name, status, screenshot, log_lines, duration_ms)
    VALUES (@job_id, @step_index, @step_name, @status, @screenshot, @log_lines, @duration_ms)
  `).run(step);
}

export function getJobSteps(jobId) {
  return getDb().prepare(
    'SELECT * FROM job_steps WHERE job_id = ? ORDER BY step_index ASC'
  ).all(jobId);
}

// ── Drive Asset Catalogue helpers (Local SQLite) ───────────────────────────

export function getDriveAsset(entityType, entityId, assetType) {
  return getDb().prepare(
    'SELECT drive_file_id, drive_folder_id, canonical_name FROM drive_assets ' +
    'WHERE entity_type=? AND entity_id=? AND asset_type=?'
  ).get(entityType, entityId, assetType) ?? null;
}

export function upsertDriveAsset({ entityType, entityId, assetType,
                                    driveFileId = null, driveFolderId = null,
                                    canonicalName = null }) {
  getDb().prepare(`
    INSERT INTO drive_assets
      (entity_type, entity_id, asset_type,
       drive_file_id, drive_folder_id, canonical_name, last_synced_at)
    VALUES (@entityType, @entityId, @assetType,
            @driveFileId, @driveFolderId, @canonicalName, datetime('now'))
    ON CONFLICT(entity_type, entity_id, asset_type) DO UPDATE SET
      drive_file_id   = COALESCE(@driveFileId,   drive_file_id),
      drive_folder_id = COALESCE(@driveFolderId, drive_folder_id),
      canonical_name  = COALESCE(@canonicalName, canonical_name),
      last_synced_at  = datetime('now')
  `).run({ entityType, entityId, assetType, driveFileId, driveFolderId, canonicalName });
}

export function listDriveAssets(entityType) {
  return getDb().prepare(
    'SELECT entity_id, asset_type, drive_file_id, drive_folder_id, canonical_name ' +
    'FROM drive_assets WHERE entity_type=? ORDER BY entity_id'
  ).all(entityType);
}

export function deleteDriveAsset(entityType, entityId, assetType = null) {
  if (assetType) {
    getDb().prepare(
      'DELETE FROM drive_assets WHERE entity_type=? AND entity_id=? AND asset_type=?'
    ).run(entityType, entityId, assetType);
  } else {
    getDb().prepare(
      'DELETE FROM drive_assets WHERE entity_type=? AND entity_id=?'
    ).run(entityType, entityId);
  }
}

// ── Serial (PRFL-NNN) helpers ──────────────────────────────────────────────

function _resolveDbId(sessionId) {
  const bare = sessionId.split('@')[0];
  if (_sessions.has(bare)) return bare;
  const withDots = bare.replace(/_/g, '.');
  if (withDots !== bare && _sessions.has(withDots)) return withDots;
  const withUnder = bare.replace(/\./g, '_');
  if (withUnder !== bare && _sessions.has(withUnder)) return withUnder;
  return bare;
}

export function getSessionSerial(sessionId) {
  const bare = sessionId.split('@')[0];
  const alt = bare.includes('.') ? bare.replace(/\./g, '_') : bare.replace(/_/g, '.');

  const s1 = _sessions.get(bare)?.serial ?? null;
  const s2 = (alt !== bare) ? (_sessions.get(alt)?.serial ?? null) : null;

  if (s1 != null && s2 != null) return Math.min(s1, s2);
  return s1 ?? s2 ?? null;
}

export async function assignSessionSerial(sessionId) {
  const slug = _resolveDbId(sessionId);

  // ── Check in-memory map first (fast path) ──────────────────────────────
  let existing = _sessions.get(slug);
  if (existing?.serial != null) return existing.serial;

  // ── Also check the alt form (dot ↔ underscore) ─────────────────────────
  const alt = slug.includes('.') ? slug.replace(/\./g, '_') : slug.replace(/_/g, '.');
  if (alt !== slug) {
    const altEntry = _sessions.get(alt);
    if (altEntry?.serial != null) {
      // Adopt the alt-form serial — record under the canonical slug too
      if (!existing) { existing = { id: slug }; _sessions.set(slug, existing); }
      existing.serial = altEntry.serial;
      return altEntry.serial;
    }
  }

  // ── Live Supabase probe: serial must survive wipes ──────────────────────
  // _sessions is populated at initDb time. If a session was deleted and
  // re-inserted after initDb ran (e.g. during wipe + re-auth) the map will be
  // stale. A D1 probe ensures we never re-assign a serial, preserving PRFL-NNN permanence.
  if (_d1) {
    try {
      const probeRow = await _d1.selectOne('sessions', 'id=?', [slug], 'id,serial');
      if (probeRow?.serial != null) {
        const preserved = probeRow.serial;
        log.info(`[serial] Preserved PRFL-${String(preserved).padStart(3,'0')} for ${slug} from live D1 probe`);
        if (!existing) { existing = { id: slug }; _sessions.set(slug, existing); }
        existing.serial = preserved;
        return preserved;
      }
    } catch (_probeErr) {
      log.warn(`[serial] D1 probe failed for ${slug}: ${_probeErr.message?.slice(0, 80)}`);
    }
  }

  // ── Truly new session: assign next serial ──────────────────────────────
  let max = 0;
  for (const s of _sessions.values()) {
    if (s.serial > max) max = s.serial;
  }
  const next = max + 1;
  log.info(`[serial] New serial PRFL-${String(next).padStart(3,'0')} assigned to ${slug}`);

  if (existing) {
    existing.serial = next;
    _supa.update('sessions', { serial: next }, ['id', slug]);
  }

  return next;
}

// ── Session State — D1 (cookie_state column in sessions table) ───────────
// Pushes/pulls cookie_state + state_pushed_at columns via D1 HTTP API.

// ── service_sessions In-Memory Map ────────────────────────────────────────
const _serviceSessions = new Map(); // 'session_id:service' -> object

/**
 * Push a full v2 session state JSON to D1 sessions.cookie_state column.
 * @param {string} sessionId  — slug or email (email normalised to slug)
 * @param {object} stateJson  — parsed v2 session object { _version, cookies, ... }
 * @returns {{ ok: boolean, bytes: number }}
 */
export async function pushSessionState(sessionId, stateJson) {
  if (!_d1) return { ok: false, bytes: 0 };
  const slug = sessionId.includes('@') ? sessionId.split('@')[0] : sessionId;
  try {
    const body = JSON.stringify(stateJson);
    await _d1.update('sessions',
      { cookie_state: body, state_pushed_at: new Date().toISOString() },
      'id=?', [slug]
    );
    log.info(`[db] pushSessionState: ${slug} → D1 (${body.length}b)`);
    return { ok: true, bytes: body.length };
  } catch (e) {
    log.warn(`[db] pushSessionState error: ${e.message}`);
    return { ok: false, bytes: 0 };
  }
}

/**
 * Pull session state from D1 sessions.cookie_state column.
 * Returns parsed state object, or null if not found. Callers fall back to Drive.
 * @param {string} sessionId  — slug or email
 * @returns {object|null}
 */
export async function pullSessionState(sessionId) {
  if (!_d1) return null;
  const slug = sessionId.includes('@') ? sessionId.split('@')[0] : sessionId;
  try {
    const row = await _d1.selectOne('sessions', 'id=?', [slug], 'cookie_state,state_pushed_at');
    if (!row?.cookie_state) return null;
    const state = typeof row.cookie_state === 'string'
      ? JSON.parse(row.cookie_state) : row.cookie_state;
    log.info(`[db] pullSessionState: ${slug} from D1 (pushed ${row.state_pushed_at})`);
    return state;
  } catch (e) {
    log.warn(`[db] pullSessionState error: ${e.message}`);
    return null;
  }
}

// ── Distributed Session Lock (D1-backed) ─────────────────────────────────
// Cross-node mutual exclusion for shared Chrome profiles / sign-in flows.
// Falls back silently when D1 is unavailable (returns true = proceed unlocked).
//
// Table: locks(key, node, acquired_at, expires_at)
// Key format: 'session:{sessionId}'   TTL: ttlMs — stale locks auto-overridden.

/**
 * Try to acquire a distributed lock for sessionId.
 * Returns true if lock acquired, false if currently held by another node.
 */
export async function acquireSessionLock(sessionId, nodeName, ttlMs = 120_000) {
  if (!_d1) return true; // D1 not initialised — proceed unlocked
  const key = `session:${sessionId}`;
  const ttlSec = Math.ceil(ttlMs / 1000);
  try {
    const acquired = await _d1.acquireLock(key, nodeName, ttlSec);
    if (!acquired) {
      log.info(`[lock] ${sessionId} locked by another node`);
    } else {
      log.info(`[lock] Acquired: ${sessionId} → ${nodeName} (ttl=${ttlMs}ms)`);
    }
    return acquired;
  } catch (e) {
    log.warn(`[lock] acquireSessionLock error: ${e.message} — proceeding unlocked`);
    return true;
  }
}

export async function releaseSessionLock(sessionId, nodeName) {
  if (!_d1) return;
  try {
    await _d1.releaseLock(`session:${sessionId}`, nodeName);
    log.info(`[lock] Released: ${sessionId}`);
  } catch (e) {
    log.warn(`[lock] releaseSessionLock error: ${e.message}`);
  }
}

export async function isSessionLocked(sessionId) {
  if (!_d1) return { locked: false };
  try {
    const key = `session:${sessionId}`;
    const now = Date.now() / 1000;
    const rows = await _d1.query(
      'SELECT node, expires_at FROM locks WHERE key=? AND expires_at > ?',
      [key, now]
    );
    if (!rows.length) return { locked: false };
    return { locked: true, node_name: rows[0].node, expires_at: rows[0].expires_at };
  } catch { return { locked: false }; }
}

// ── service_sessions helpers (universal service credential tracker) ──────────
// Tracks stored state/credentials for any service (tailscale, v0, github, etc.)
// per profile slot. Designed for extensibility — any service can be tracked.

/**
 * Upsert a service session record.
 * @param {object} opts
 * @param {string} opts.session_id   - PRFL slug or 'mesh-admin'
 * @param {string} opts.service      - service identifier ('tailscale','v0','google', etc.)
 * @param {string} [opts.account_hint] - username/email used
 * @param {boolean} [opts.state_valid] - true if verified valid
 * @param {string} [opts.node_name]  - which node saved this
 * @param {string} [opts.storage_path] - Drive path, R2 key, or local path
 * @param {object} [opts.metadata]   - arbitrary service-specific extras
 */
export function upsertServiceSession({ session_id, service, account_hint, state_valid, node_name, storage_path, metadata }) {
  const key = `${session_id}:${service}`;
  const now = new Date().toISOString();
  const isValid = state_valid !== undefined ? Boolean(state_valid) : undefined;

  const existing = _serviceSessions.get(key) ?? {};
  const obj = {
    ...existing,
    session_id,
    service,
    ...(account_hint  !== undefined ? { account_hint }  : {}),
    ...(isValid       !== undefined ? { state_valid: isValid ? 1 : 0 } : {}),
    ...(node_name     !== undefined ? { node_name }     : {}),
    ...(storage_path  !== undefined ? { storage_path }  : {}),
    ...(metadata      !== undefined ? { metadata: typeof metadata === 'string' ? metadata : JSON.stringify(metadata) } : {}),
    ...(isValid === true            ? { last_verified: now, last_saved_at: now } : {}),
  };

  _serviceSessions.set(key, obj);

  // Mirror to local SQLite
  try {
    getDb().prepare(`
      INSERT INTO service_sessions
        (session_id, service, account_hint, state_valid, last_verified, last_saved_at, node_name, storage_path, metadata)
      VALUES (@session_id, @service, @account_hint, @state_valid, @last_verified, @last_saved_at, @node_name, @storage_path, @metadata)
      ON CONFLICT(session_id, service) DO UPDATE SET
        account_hint  = COALESCE(@account_hint,  account_hint),
        state_valid   = COALESCE(@state_valid,   state_valid),
        last_verified = COALESCE(@last_verified, last_verified),
        last_saved_at = COALESCE(@last_saved_at, last_saved_at),
        node_name     = COALESCE(@node_name,     node_name),
        storage_path  = COALESCE(@storage_path,  storage_path),
        metadata      = COALESCE(@metadata,      metadata)
    `).run({
      session_id,
      service,
      account_hint:  obj.account_hint  ?? null,
      state_valid:   obj.state_valid   ?? 0,
      last_verified: obj.last_verified ?? null,
      last_saved_at: obj.last_saved_at ?? null,
      node_name:     obj.node_name     ?? null,
      storage_path:  obj.storage_path  ?? null,
      metadata:      obj.metadata      ?? null,
    });
  } catch (dbe) {
    log.warn(`[db] upsertServiceSession SQLite error: \${dbe.message}`);
  }

  // Mirror to Supabase asynchronously
  _supa.upsert('service_sessions', {
    session_id,
    service,
    account_hint:  obj.account_hint  ?? null,
    state_valid:   Boolean(obj.state_valid),
    last_verified: obj.last_verified ?? null,
    last_saved_at: obj.last_saved_at ?? null,
    node_name:     obj.node_name     ?? null,
    storage_path:  obj.storage_path  ?? null,
    metadata:      obj.metadata ? (typeof obj.metadata === 'string' ? JSON.parse(obj.metadata) : obj.metadata) : null,
  });
}

/**
 * Get a single service session record.
 */
export function getServiceSession(sessionId, service) {
  const slug = sessionId.includes('@') ? sessionId.split('@')[0] : sessionId;
  return _serviceSessions.get(`${slug}:${service}`) ?? null;
}

/**
 * List all service sessions, optionally filtered by session_id or service.
 */
export function listServiceSessions({ session_id, service } = {}) {
  const arr = Array.from(_serviceSessions.values());
  return arr.filter(s =>
    (!session_id || s.session_id === session_id) &&
    (!service    || s.service    === service)
  );
}

/**
 * Mark a service session as valid or invalid.
 */
export function markServiceSessionValid(sessionId, service, valid, nodeName) {
  const slug = sessionId.includes('@') ? sessionId.split('@')[0] : sessionId;
  upsertServiceSession({
    session_id:  slug,
    service,
    state_valid: Boolean(valid),
    node_name:   nodeName ?? null,
  });
}

// ── node_ts_states helpers ──────────────────────────────────────────────────

export function upsertNodeTsState({ nodeName, stateB64, stateSize, tsIp, sessionId, runtimeId }) {
  const now = new Date().toISOString();
  // Coerce undefined → null for better-sqlite3 (throws on undefined bind params)
  const _b64 = stateB64 ?? null;
  const _sz  = stateSize ?? 0;
  const _ip  = tsIp ?? '';
  const _sid = sessionId ?? '';
  const _rid = runtimeId ?? '';
  try {
    getDb().prepare(`
      INSERT INTO node_ts_states (node_name, state_b64, state_size, ts_ip, session_id, runtime_id, updated_at)
      VALUES (@nodeName, @_b64, @_sz, @_ip, @_sid, @_rid, @now)
      ON CONFLICT(node_name) DO UPDATE SET
        state_b64  = COALESCE(@_b64,  state_b64),
        state_size = COALESCE(@_sz,   state_size),
        ts_ip      = COALESCE(@_ip,   ts_ip),
        session_id = COALESCE(@_sid,  session_id),
        runtime_id = COALESCE(@_rid,  runtime_id),
        updated_at = @now
    `).run({ nodeName, _b64, _sz, _ip, _sid, _rid, now });
  } catch (dbe) {
    log.warn(`[db] upsertNodeTsState SQLite error: ${dbe.message}`);
  }

  // NOTE: TS state is persisted to R2 ts_states/ by boot.py keep-alive and
  // sync.py. No D1 table for it. The local SQLite upsert above is the only
  // local cache — R2 is the cross-runtime primary store.
}

export async function getNodeTsState(nodeName) {
  try {
    const local = getDb().prepare('SELECT * FROM node_ts_states WHERE node_name = ?').get(nodeName);
    if (local && local.state_b64) return local;
  } catch {}
  // R2 is the primary store — caller (boot.py / sync.py) reads R2 directly.
  return null;
}

export async function queryD1(sql, params = []) {
  if (!_d1) throw new Error('D1 not initialized');
  return await _d1.query(sql, params);
}
