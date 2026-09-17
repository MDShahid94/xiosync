// ─── Session Manager ──────────────────────────────────────────────────────
// Handles storageState (cookies + localStorage + IndexedDB) serialization.
// Chrome profiles (full user-data-dir) are stored as trimmed .tar.gz on R2 (primary) and Drive (cold backup).
//
// Session file format v2:
//   { _version: 2, _captured_at, _capture_method, _domains[],
//     cookies[], origins[], indexedDB[] }
//
// v2 files are self-sufficient for cross-runtime session survival without
// the .tar.gz Chrome profile tarball.

import path from 'node:path';
import fs from 'node:fs';
import { execSync } from 'node:child_process';
import { Paths, ensureDir } from '../utils/drive.mjs';
import { createLogger } from '../utils/logger.mjs';
import { getDb, getSessionSerial, assignSessionSerial, setSessionPersisted } from './db.mjs';
import { getAllMidAuthFragments } from './ecosystem.mjs';

const log = createLogger('session-mgr');

// ── Resolved once at module load — avoids repeated env reads ─────────────────
const CHROME_PROFILES_BASE = process.env.CHROME_PROFILES_DIR ?? '/content/xio-mesh/chrome_profiles';

// ── Soft-persistence config (loaded from /tmp/xio_config.json) ──────────────
// Defaults are sensible; overridden by start.ipynb Cell 1 config.
let _softPersistConfig = null;
function _getSoftPersistConfig() {
  if (_softPersistConfig) return _softPersistConfig;
  const defaults = {
    indexeddb_origins: [
      'https://accounts.google.com',
      'https://myaccount.google.com',
      'https://www.google.com',
      'https://v0.dev',
    ],
    localstorage_min_cookies: 5,
    warmup_origins: [
      'https://accounts.google.com',
      'https://myaccount.google.com',
    ],
  };
  try {
    const cfg = JSON.parse(fs.readFileSync('/tmp/xio_config.json', 'utf8'));
    _softPersistConfig = {
      indexeddb_origins:        cfg.indexeddb_origins        ?? defaults.indexeddb_origins,
      localstorage_min_cookies: cfg.localstorage_min_cookies ?? defaults.localstorage_min_cookies,
      warmup_origins:           cfg.warmup_origins           ?? defaults.warmup_origins,
    };
  } catch {
    _softPersistConfig = defaults;
  }
  return _softPersistConfig;
}
export { _getSoftPersistConfig as getSoftPersistConfig };

// ── Naming helpers (wrap naming.py conventions in JS) ──────────────────────

/**
 * Get or assign a serial for a session, then return the canonical base name.
 * Pattern: PRFL-003_karmareturnsfromallsides
 * Falls back to bare sessionId if DB is not ready yet.
 */
function _canonicalBase(sessionId) {
  // Guard: sessionId must be a non-empty string. A JS object stringified as
  // "[object Object]" would produce corrupt filenames like PRFL-063_object_object.json.
  if (typeof sessionId !== 'string' || !sessionId.trim()) {
    throw new Error(`_canonicalBase: invalid sessionId (${typeof sessionId}): ${JSON.stringify(sessionId)}`);
  }
  try {
    const serial = getSessionSerial(sessionId) ?? assignSessionSerial(sessionId);
    const username = sessionId.split('@')[0].replace(/[^a-zA-Z0-9]/g, '_').toLowerCase();
    return `PRFL-${String(serial).padStart(3, '0')}_${username}`;
  } catch {
    return sessionId; // DB not ready — use legacy name
  }
}

// ── storageState (lightweight — cookies + localStorage only) ───────────────

export function sessionStatePath(sessionId) {
  const base = _canonicalBase(sessionId);
  const canon = path.join(Paths.sessions(), `${base}.json`);
  // Also check legacy path for backward compat
  const legacy = path.join(Paths.sessions(), `${sessionId}.json`);
  if (!fs.existsSync(canon) && fs.existsSync(legacy)) return legacy;
  return canon;
}

export function loadStorageState(sessionId) {
  const p = sessionStatePath(sessionId);
  if (!fs.existsSync(p)) return null;
  try {
    const state = JSON.parse(fs.readFileSync(p, 'utf8'));
    // Sanitize partitionKey: CDP (Chrome 119+) stores it as an object
    // {topLevelSite, hasCrossSiteAncestor}. Playwright requires a string or absent.
    // Fix legacy session files transparently on every read.
    if (Array.isArray(state.cookies)) {
      state.cookies = state.cookies.map(c => {
        if (c.partitionKey !== undefined && typeof c.partitionKey === 'object' && c.partitionKey !== null) {
          const pk = c.partitionKey.topLevelSite ?? undefined;
          const { partitionKey: _drop, ...rest } = c;
          return pk ? { ...rest, partitionKey: pk } : rest;
        }
        return c;
      });
    }
    return state;
  } catch (e) {
    log.warn(`Failed to load session state ${sessionId}: ${e.message}`);
    return null;
  }
}

// ── Cookie merge helpers ────────────────────────────────────────────────────

/**
 * Unique key for a cookie — dedup by (domain, name, path).
 */
function _cookieKey(c) { return `${c.domain}\0${c.name}\0${c.path ?? '/'}`; }

/**
 * Merge two cookie arrays. For each unique (domain, name, path):
 *   - Keep the cookie with the LONGER remaining TTL
 *   - If equal, prefer the newer capture (from `incoming`)
 * Returns { merged, stats: { kept, refreshed, added } }
 */
function _mergeCookies(existing, incoming) {
  const map = new Map();
  const nowSec = Date.now() / 1000;

  // Seed with existing cookies
  for (const c of (existing ?? [])) {
    map.set(_cookieKey(c), c);
  }

  let refreshed = 0, added = 0;
  for (const c of (incoming ?? [])) {
    const key = _cookieKey(c);
    const prev = map.get(key);
    if (!prev) {
      map.set(key, c);
      added++;
    } else {
      // Keep whichever has longer remaining TTL
      const prevTTL = (prev.expires ?? -1) <= 0 ? Infinity : prev.expires - nowSec;
      const newTTL  = (c.expires ?? -1) <= 0    ? Infinity : c.expires - nowSec;
      if (newTTL >= prevTTL) {
        map.set(key, c);
        refreshed++;
      }
    }
  }

  return {
    merged: [...map.values()],
    stats: { kept: map.size - added - refreshed, refreshed, added },
  };
}

/**
 * Merge two origins[] arrays. For each unique origin, prefer the one with
 * more localStorage entries.
 */
function _mergeOrigins(existing, incoming) {
  const map = new Map();
  for (const o of (existing ?? [])) map.set(o.origin, o);
  for (const o of (incoming ?? [])) {
    const prev = map.get(o.origin);
    if (!prev || (o.localStorage?.length ?? 0) >= (prev.localStorage?.length ?? 0)) {
      map.set(o.origin, o);
    }
  }
  return [...map.values()];
}

/**
 * Merge two indexedDB[] arrays. For each unique origin, prefer the one with
 * more databases/entries.
 */
function _mergeIndexedDB(existing, incoming) {
  const map = new Map();
  for (const o of (existing ?? [])) map.set(o.origin, o);
  for (const o of (incoming ?? [])) {
    const prev = map.get(o.origin);
    if (!prev || (o.databases?.length ?? 0) >= (prev.databases?.length ?? 0)) {
      map.set(o.origin, o);
    }
  }
  return [...map.values()];
}

/**
 * Returns true when the cookies array contains at least one non-expired cookie
 * whose domain matches the apex domain of the given origin URL.
 *
 * Used to decide whether it is safe to navigate to an origin during hydration.
 * Expired-session navigations trigger security challenges (Google, v0, etc.) that
 * can destabilize the Chromium context.
 *
 * A cookie is considered "valid" if it is a session cookie (expires=-1/0) OR
 * its expiry is more than `bufferSec` seconds in the future.
 *
 * Apex-domain matching:
 *   'accounts.google.com' → apex 'google.com'
 *   'v0.dev'              → apex 'v0.dev'
 *   '.google.com' cookie  → matches apex 'google.com' ✓
 *
 * @param {Array}  cookies     - cookies[] from a session file
 * @param {string} originUrl   - e.g. 'https://accounts.google.com'
 * @param {number} [bufferSec=300] - treat cookies expiring within this window as expired
 */
function _hasValidCookiesForOrigin(cookies, originUrl, bufferSec = 300) {
  if (!cookies?.length) return false;
  let host;
  try { host = new URL(originUrl).hostname; } catch { return false; }
  const apex   = host.split('.').slice(-2).join('.');
  const nowSec = Date.now() / 1000;
  return cookies.some(c => {
    const cd = (c.domain ?? '').replace(/^\./, '');
    if (!cd.endsWith(apex)) return false;       // wrong domain — skip
    if ((c.expires ?? -1) <= 0) return true;   // session cookie — always valid
    return c.expires > nowSec + bufferSec;     // persistent cookie with remaining TTL
  });
}

/**
 * Returns true when the cookies array contains ANY cookie (expired or not)
 * for the apex domain of the given origin URL.
 *
 * Combined with _hasValidCookiesForOrigin, this lets us distinguish:
 *   • domain has cookies, all valid   → navigate + inject ✅
 *   • domain has no cookies at all    → navigate + inject ✅ (cookie-free service)
 *   • domain has cookies, all expired → SKIP navigation   ❌ (security challenge risk)
 */
function _hasCookiesForOrigin(cookies, originUrl) {
  if (!cookies?.length) return false;
  let host;
  try { host = new URL(originUrl).hostname; } catch { return false; }
  const apex = host.split('.').slice(-2).join('.');
  return cookies.some(c => (c.domain ?? '').replace(/^\./, '').endsWith(apex));
}


// ── Mid-auth URL guard ────────────────────────────────────────────────────

/**
 * Returns true if the given URL indicates the browser is in the middle of an
 * OAuth / sign-in redirect flow. Saving Google cookies from these pages can
 * silently replace valid rotating tokens (SIDCC, __Secure-1PSIDTS, etc.) with
 * invalidated-but-longer-TTL ones written by Google's redirect machinery.
 *
 * @param {string|null|undefined} url
 * @returns {boolean}
 */
function _isMidAuthUrl(url) {
  if (!url) return false;
  try {
    return getAllMidAuthFragments().some(frag => url.includes(frag));
  } catch {
    // Fallback hard-coded list if ecosystem.mjs fails to load
    return [
      'accounts.google.com/v3/signin',
      'accounts.google.com/signin/v2',
      'accounts.google.com/o/oauth2',
      'login.tailscale.com',
    ].some(frag => url.includes(frag));
  }
}

/**
 * Returns false if the incoming Google SID cookie has a significantly shorter
 * TTL than the existing one — a sign that Google issued a fresh/invalidated SID
 * from a login redirect (same key, but the value is now invalid).
 *
 * "Significantly shorter" = existing expires more than 7 days sooner than incoming.
 * We use 7 days to allow legitimate token renewals (which usually extend TTL).
 *
 * @param {object[]} existingCookies
 * @param {object[]} incomingCookies
 * @returns {boolean} true = safe to merge, false = incoming looks degraded
 */
function _isSafeGoogleCookieUpdate(existingCookies, incomingCookies) {
  const googleSID = (cookies) =>
    (cookies ?? []).find(c => c.name === 'SID' && (c.domain ?? '').includes('google'));
  const existSID = googleSID(existingCookies);
  const incomSID = googleSID(incomingCookies);
  // If no SID in existing file — nothing to protect
  if (!existSID) return true;
  // If incoming has no SID at all — it wiped the session; refuse
  if (!incomSID) return false;
  // Session cookies (expiry -1 or 0) are always safe to replace
  if (existSID.expires <= 0 || incomSID.expires <= 0) return true;
  const nowSec   = Date.now() / 1000;
  const existTTL = existSID.expires > 0 ? existSID.expires - nowSec : Infinity;
  const incomTTL = incomSID.expires > 0 ? incomSID.expires - nowSec : Infinity;
  // Incoming SID expires more than 7 days sooner → looks like an invalidated token
  const THRESHOLD_SEC = 7 * 24 * 3600;
  if (existTTL - incomTTL > THRESHOLD_SEC) {
    log.warn(`saveStorageState: incoming SID TTL (${Math.round(incomTTL/3600)}h) is ` +
      `${Math.round((existTTL-incomTTL)/3600)}h shorter than existing — skipping Google cookie update`);
    return false;
  }
  return true;
}


// ── saveStorageState (with merge-save anti-degradation) ─────────────────────

/**
 * Save storageState from a Playwright BrowserContext OR a plain state object.
 *
 * Accepts:
 *   saveStorageState(id, ctx.context)        — BrowserContext (async): calls .storageState() internally
 *   saveStorageState(id, stateObj)           — Plain { cookies, origins, ... } object (sync-safe)
 *   saveStorageState(id, stateObj, pageUrl)  — Also pass current page URL to enable mid-auth guard
 *
 * Anti-degradation:
 *   1. Merges incoming cookies with existing — longer TTL always wins.
 *   2. Mid-auth URL guard: if pageUrl is on a Google/Tailscale OAuth page,
 *      skips updating Google cookies (keeps existing valid tokens intact).
 *   3. SID-TTL safety: refuses to replace a valid Google SID with one that
 *      expires significantly sooner (invalidated redirect token).
 *
 * Always returns a Promise so callers can safely await it.
 */
export async function saveStorageState(sessionId, stateOrContext, pageUrl = null) {
  ensureDir(Paths.sessions());
  const p = sessionStatePath(sessionId);

  let incoming = stateOrContext;
  // Detect BrowserContext: it has a .storageState() method
  if (stateOrContext && typeof stateOrContext.storageState === 'function') {
    incoming = await stateOrContext.storageState();
  }

  if (!incoming || (Array.isArray(incoming.cookies) && incoming.cookies.length === 0 && !incoming.origins?.length)) {
    log.warn(`saveStorageState: state for ${sessionId} has no cookies — skipping to protect existing file`);
    return;
  }

  // ── Mid-auth URL guard ────────────────────────────────────────────────────
  // If the browser is currently on an OAuth redirect/challenge page, Google may
  // have written invalidated-but-longer-TTL tokens. Skip Google-cookie updates.
  const midAuth = _isMidAuthUrl(pageUrl);
  if (midAuth) {
    log.warn(`saveStorageState: pageUrl '${(pageUrl ?? '').slice(0, 80)}' is mid-auth — ` +
      `skipping Google cookie update to protect existing valid tokens`);
  }

  // ── Merge with existing file ──────────────────────────────────────────────
  const existing = loadStorageState(sessionId);
  let finalState;

  if (existing && (existing.cookies?.length ?? 0) > 0) {
    // When mid-auth, only use non-Google cookies from incoming (preserve existing Google cookies)
    const incomingToMerge = midAuth
      ? (incoming.cookies ?? []).filter(c => !(c.domain ?? '').includes('google'))
      : (incoming.cookies ?? []);

    // SID-TTL safety: protect against invalidated redirect tokens replacing valid SID
    const effectiveCookies = (!midAuth && !_isSafeGoogleCookieUpdate(existing.cookies, incoming.cookies))
      ? (incoming.cookies ?? []).filter(c => !(c.domain ?? '').includes('google'))
      : incomingToMerge;

    const { merged, stats } = _mergeCookies(existing.cookies, effectiveCookies);
    const mergedOrigins     = _mergeOrigins(existing.origins, incoming.origins);
    const mergedIndexedDB   = _mergeIndexedDB(existing.indexedDB, incoming.indexedDB);

    finalState = {
      ...(existing._version ? { _version: existing._version } : {}),
      ...(existing._captured_at ? { _captured_at: existing._captured_at } : {}),
      ...(existing._capture_method ? { _capture_method: existing._capture_method } : {}),
      cookies:   merged,
      origins:   mergedOrigins,
      ...(mergedIndexedDB.length > 0 ? { indexedDB: mergedIndexedDB } : {}),
    };

    log.info(`Session state merged: ${sessionId} (${existing.cookies.length} existing + ${effectiveCookies.length} effective new → ${merged.length} unique | refreshed=${stats.refreshed} added=${stats.added}${midAuth ? ' [mid-auth guard active]' : ''})`);
  } else {
    finalState = incoming;
    log.info(`Session state saved: ${sessionId} (${incoming?.cookies?.length ?? 0} cookies, fresh write)`);
  }

  fs.writeFileSync(p, JSON.stringify(finalState, null, 2));

  // Mark as persisted (locally saved — Drive push happens separately in google-signin Phase 3)
  try {
    setSessionPersisted(sessionId.split('@')[0], true);
  } catch (dbe) {
    log.warn(`saveStorageState: could not set is_persisted: ${dbe.message}`);
  }
}

// ── saveStorageStateFull (CDP-based rich capture for login-time) ─────────────

/**
 * Rich CDP-based capture: saves ALL cookies from ALL domains in the browser
 * (via Storage.getCookies / Network.getAllCookies), plus localStorage and
 * IndexedDB for configurable origins.
 *
 * This produces a v2 session file that is self-sufficient for cross-runtime
 * survival without the .tar.gz Chrome profile tarball.
 *
 * Call this ONLY at login-time (google-signin Phase 3) — not on every job.
 *
 * @param {string}  sessionId  Session slot ID
 * @param {object}  page       Playwright Page object (used for CDP and evaluate)
 */
export async function saveStorageStateFull(sessionId, page) {
  ensureDir(Paths.sessions());
  const p = sessionStatePath(sessionId);
  const cfg = _getSoftPersistConfig();

  let allCookies = [];
  let origins = [];
  let indexedDBData = [];

  // ── 1. Get ALL cookies via CDP (includes unvisited domains) ──────────────
  try {
    const cdp = await page.context().newCDPSession(page);
    // Try modern Storage.getCookies first, fall back to deprecated Network.getAllCookies
    let cookieResult;
    try {
      cookieResult = await cdp.send('Storage.getCookies');
    } catch {
      cookieResult = await cdp.send('Network.getAllCookies');
    }
    const rawCookies = cookieResult?.cookies ?? [];

    // Map ALL CDP fields — preserve everything for accurate restore
    allCookies = rawCookies.map(c => {
      // CDP (Chrome 119+) returns partitionKey as an object {topLevelSite, hasCrossSiteAncestor}.
      // Playwright's storageState requires partitionKey to be a string or absent.
      // Strip object-typed partitionKey to avoid "expected string, got object" error.
      const pk = c.partitionKey;
      const partitionKeyStr = typeof pk === 'string' ? pk
        : typeof pk === 'object' && pk !== null ? (pk.topLevelSite ?? undefined)
        : undefined;
      return {
        name:         c.name,
        value:        c.value,
        domain:       c.domain,
        path:         c.path ?? '/',
        expires:      c.expires ?? -1,
        httpOnly:     c.httpOnly ?? false,
        secure:       c.secure ?? false,
        sameSite:     c.sameSite ?? 'None',
        ...(partitionKeyStr          ? { partitionKey: partitionKeyStr } : {}),
        ...(c.sourceScheme           ? { sourceScheme: c.sourceScheme } : {}),
        ...(c.sourcePort             ? { sourcePort: c.sourcePort }     : {}),
        ...(c.priority               ? { priority: c.priority }         : {}),
      };
    });

    await cdp.detach();
    log.info(`saveStorageStateFull: CDP captured ${allCookies.length} cookies for ${sessionId}`);
  } catch (cdpErr) {
    log.warn(`saveStorageStateFull: CDP failed (${cdpErr.message}), falling back to storageState()`);
    const fallback = await page.context().storageState();
    allCookies = fallback.cookies ?? [];
    origins    = fallback.origins ?? [];
  }

  // ── 2. Determine which origins need localStorage capture ──────────────────
  // Heuristic: capture for origins with ≥ N cookies (configurable)
  const minCookies = cfg.localstorage_min_cookies;
  const cookieCountByOrigin = new Map();
  for (const c of allCookies) {
    // Convert cookie domain to origin: '.google.com' → multiple possible origins
    const domain = c.domain?.startsWith('.') ? c.domain.slice(1) : c.domain;
    const origin = `https://${domain}`;
    cookieCountByOrigin.set(origin, (cookieCountByOrigin.get(origin) ?? 0) + 1);
  }

  // Merge: configurable origins + origins with enough cookies
  const lsOrigins = new Set([
    ...cfg.warmup_origins,
    ...[...cookieCountByOrigin.entries()]
      .filter(([, count]) => count >= minCookies)
      .map(([origin]) => origin),
  ]);

  // ── 3. Capture localStorage from each origin ──────────────────────────────
  for (const origin of lsOrigins) {
    try {
      await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 8000 });
      // Brief settle for SPA hydration
      await new Promise(r => setTimeout(r, 800));
      const ls = await page.evaluate(() => {
        const items = [];
        for (let i = 0; i < localStorage.length; i++) {
          const name = localStorage.key(i);
          items.push({ name, value: localStorage.getItem(name) });
        }
        return items;
      });
      if (ls.length > 0) {
        origins.push({ origin, localStorage: ls });
        log.info(`  localStorage: ${origin} → ${ls.length} entries`);
      }
    } catch (e) {
      log.warn(`  localStorage capture failed for ${origin}: ${e.message}`);
    }
  }

  // ── 4. Capture IndexedDB for allowlisted origins ──────────────────────────
  for (const origin of cfg.indexeddb_origins) {
    try {
      const cdp2 = await page.context().newCDPSession(page);
      // Navigate to the origin first so CDP context is correct
      await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 8000 });
      await new Promise(r => setTimeout(r, 500));

      const { databaseNames } = await cdp2.send('IndexedDB.requestDatabaseNames', {
        securityOrigin: origin,
      });

      if (databaseNames?.length > 0) {
        const databases = [];
        for (const dbName of databaseNames) {
          try {
            const dbInfo = await cdp2.send('IndexedDB.requestDatabase', {
              securityOrigin: origin,
              databaseName: dbName,
            });
            const objectStores = [];
            for (const store of (dbInfo.databaseWithObjectStores?.objectStores ?? [])) {
              try {
                const dataResult = await cdp2.send('IndexedDB.requestData', {
                  securityOrigin: origin,
                  databaseName: dbName,
                  objectStoreName: store.name,
                  indexName: '',
                  skipCount: 0,
                  pageSize: 100,  // cap at 100 entries per store
                });
                const entries = (dataResult.objectStoreDataEntries ?? []).map(e => ({
                  key: JSON.stringify(e.key),
                  value: JSON.stringify(e.value),
                }));
                if (entries.length > 0) {
                  objectStores.push({ name: store.name, keyPath: store.keyPath, entries });
                }
              } catch { /* skip stores that fail */ }
            }
            if (objectStores.length > 0) {
              databases.push({ name: dbName, version: dbInfo.databaseWithObjectStores?.version, objectStores });
            }
          } catch { /* skip databases that fail */ }
        }
        if (databases.length > 0) {
          indexedDBData.push({ origin, databases });
          log.info(`  IndexedDB: ${origin} → ${databases.length} databases`);
        }
      }
      await cdp2.detach();
    } catch (e) {
      log.warn(`  IndexedDB capture failed for ${origin}: ${e.message}`);
    }
  }

  // ── 5. Build v2 state and merge with existing ─────────────────────────────
  const v2State = {
    _version:         2,
    _captured_at:     new Date().toISOString(),
    _capture_method:  'cdp_full',
    _domains:         [...new Set(allCookies.map(c => c.domain))],
    cookies:          allCookies,
    origins,
    ...(indexedDBData.length > 0 ? { indexedDB: indexedDBData } : {}),
  };

  // Merge with existing file to preserve any data we didn't re-capture
  const existing = loadStorageState(sessionId);
  if (existing && (existing.cookies?.length ?? 0) > 0) {
    const { merged } = _mergeCookies(existing.cookies, v2State.cookies);
    v2State.cookies   = merged;
    v2State.origins   = _mergeOrigins(existing.origins, v2State.origins);
    if (existing.indexedDB) {
      v2State.indexedDB = _mergeIndexedDB(existing.indexedDB, v2State.indexedDB ?? []);
    }
  }

  v2State._domains = [...new Set(v2State.cookies.map(c => c.domain))];

  fs.writeFileSync(p, JSON.stringify(v2State, null, 2));
  log.info(`saveStorageStateFull: COMPLETE for ${sessionId} — ${v2State.cookies.length} cookies, ${v2State.origins?.length ?? 0} origins, ${v2State.indexedDB?.length ?? 0} IndexedDB origins`);

  // Mark as persisted
  try {
    setSessionPersisted(sessionId.split('@')[0], true);
  } catch (dbe) {
    log.warn(`saveStorageStateFull: is_persisted update failed: ${dbe.message}`);
  }

  // Fire-and-forget Supabase push: ensures the session JSON reaches primary storage
  // even if the runtime crashes before job-manager's post-job push runs.
  // awaitPrimary=false — fully detached, never blocks the caller.
  pushToStorage(sessionId, { session: true, profile: false, awaitPrimary: false })
    .catch(e => log.warn(`saveStorageStateFull: background Supabase push failed: ${e.message}`));
}


// ── loadAndHydrateContext (full restore from v2 session file) ────────────────

/**
 * Fully hydrate a Playwright BrowserContext from a v2 (or v1) session file.
 *
 * Steps:
 *   1. Inject ALL cookies via context.addCookies() (including httpOnly)
 *   2. For each saved origin: navigate, inject localStorage via evaluate()
 *   3. For each saved IndexedDB origin: navigate, inject data via evaluate()
 *   4. Warm-up navigation to trigger Google's rotating cookie renewal
 *
 * This replaces the old `storageState` context option approach, which only
 * injected cookies — not localStorage or IndexedDB.
 *
 * @param {string}  sessionId  Session slot ID
 * @param {object}  context    Playwright BrowserContext (freshly created, no storageState)
 */
export async function loadAndHydrateContext(sessionId, context) {
  const state = loadStorageState(sessionId);
  if (!state) {
    log.info(`loadAndHydrateContext: no session file for ${sessionId} — blank context`);
    return;
  }

  const cfg = _getSoftPersistConfig();

  // ── Step 1: Inject ALL cookies ────────────────────────────────────────────
  if (state.cookies?.length > 0) {
    // Playwright addCookies requires 'url' or 'domain' — our cookies have domain
    // Filter out expired cookies (negative TTL)
    const nowSec = Date.now() / 1000;
    const validCookies = state.cookies.filter(c => {
      if ((c.expires ?? -1) <= 0) return true;  // session cookies — always valid
      return c.expires > nowSec;                 // non-expired persistent cookies
    }).map(c => ({
      name:     c.name,
      value:    c.value,
      domain:   c.domain,
      path:     c.path ?? '/',
      expires:  (c.expires ?? -1) <= 0 ? undefined : c.expires,
      httpOnly: c.httpOnly ?? false,
      secure:   c.secure ?? false,
      sameSite: c.sameSite === 'None' ? 'None' : c.sameSite === 'Strict' ? 'Strict' : 'Lax',
    }));

    try {
      await context.addCookies(validCookies);
      log.info(`  Hydrate: injected ${validCookies.length} cookies (${state.cookies.length - validCookies.length} expired, skipped)`);
    } catch (e) {
      log.warn(`  Hydrate: addCookies failed: ${e.message}`);
    }
  }

  // ── Step 2: Inject localStorage per origin ────────────────────────────────
  // Guard: if the session file has cookies for an origin's domain but they are
  // ALL expired, skip navigation to that origin. Navigating with stale auth
  // cookies can trigger security challenges (Google, v0, etc.) that may
  // destabilize the Chromium context. Purely cookie-free origins are unaffected.
  if (state.origins?.length > 0) {
    for (const origin of state.origins) {
      if (!origin.localStorage?.length) continue;

      // Domain-validity guard
      const hasCookies      = _hasCookiesForOrigin(state.cookies, origin.origin);
      const hasValidCookies = _hasValidCookiesForOrigin(state.cookies, origin.origin);
      if (hasCookies && !hasValidCookies) {
        log.info(`  Hydrate: localStorage skip for ${origin.origin} — cookies exist but all expired`);
        continue;
      }

      let page;
      try {
        page = await context.newPage();
        await page.goto(origin.origin, { waitUntil: 'domcontentloaded', timeout: 8000 });
        await page.evaluate((entries) => {
          for (const { name, value } of entries) {
            try { localStorage.setItem(name, value); } catch {}
          }
        }, origin.localStorage);
        log.info(`  Hydrate: localStorage for ${origin.origin} → ${origin.localStorage.length} entries`);
      } catch (e) {
        log.warn(`  Hydrate: localStorage inject failed for ${origin.origin}: ${e.message}`);
      } finally {
        if (page) await page.close().catch(() => {});
      }
    }
  }

  // ── Step 3: Inject IndexedDB per origin ───────────────────────────────────
  // Same domain-validity guard as Step 2.
  if (state.indexedDB?.length > 0) {
    for (const idbOrigin of state.indexedDB) {
      const hasCookies      = _hasCookiesForOrigin(state.cookies, idbOrigin.origin);
      const hasValidCookies = _hasValidCookiesForOrigin(state.cookies, idbOrigin.origin);
      if (hasCookies && !hasValidCookies) {
        log.info(`  Hydrate: IndexedDB skip for ${idbOrigin.origin} — cookies exist but all expired`);
        continue;
      }

      let page;
      try {
        page = await context.newPage();
        await page.goto(idbOrigin.origin, { waitUntil: 'domcontentloaded', timeout: 8000 });

        for (const db of idbOrigin.databases) {
          await page.evaluate(({ dbName, version, objectStores }) => {
            return new Promise((resolve, reject) => {
              const req = indexedDB.open(dbName, version ?? 1);
              req.onupgradeneeded = (e) => {
                const idb = e.target.result;
                for (const store of objectStores) {
                  if (!idb.objectStoreNames.contains(store.name)) {
                    idb.createObjectStore(store.name, {
                      keyPath: store.keyPath || undefined,
                    });
                  }
                }
              };
              req.onsuccess = (e) => {
                const idb = e.target.result;
                try {
                  for (const store of objectStores) {
                    if (!idb.objectStoreNames.contains(store.name)) continue;
                    const tx = idb.transaction(store.name, 'readwrite');
                    const os = tx.objectStore(store.name);
                    for (const entry of store.entries) {
                      try {
                        os.put(JSON.parse(entry.value), JSON.parse(entry.key));
                      } catch {}
                    }
                  }
                } catch {}
                idb.close();
                resolve();
              };
              req.onerror = () => reject(req.error);
              setTimeout(resolve, 5000); // safety timeout
            });
          }, db);
        }
        log.info(`  Hydrate: IndexedDB for ${idbOrigin.origin} → ${idbOrigin.databases.length} databases`);
      } catch (e) {
        log.warn(`  Hydrate: IndexedDB inject failed for ${idbOrigin.origin}: ${e.message}`);
      } finally {
        if (page) await page.close().catch(() => {});
      }
    }
  }

  // ── Step 4: Warm-up navigation — per-origin validity check ───────────────
  // Only warm up origins where we have valid (non-expired) session cookies.
  //
  // The old guard `state.cookies.some(c => c.domain.includes('google.com'))` was
  // too broad: it triggered the warm-up even when ALL Google cookies were expired,
  // causing Google to serve a security-challenge page that crashes the context.
  //
  // New guard: _hasValidCookiesForOrigin() — per-origin apex-domain check with a
  // 5-minute buffer.  Other origins' data is NEVER affected (skipped origins are
  // simply not warmed-up; their cookies were already injected in Step 1).
  const activeWarmupOrigins = (cfg.warmup_origins ?? []).filter(origin =>
    _hasValidCookiesForOrigin(state.cookies, origin)
  );

  if (activeWarmupOrigins.length > 0) {
    let warmupPage;
    try {
      warmupPage = await context.newPage();
      for (const origin of activeWarmupOrigins) {
        try {
          await warmupPage.goto(origin, { waitUntil: 'domcontentloaded', timeout: 10000 });
          // Brief wait for server to rotate tokens (SIDCC, __Secure-1PSIDTS, etc.)
          await new Promise(r => setTimeout(r, 1500));
          log.info(`  Hydrate: warm-up ✓ ${origin}`);
        } catch (e) {
          log.warn(`  Hydrate: warm-up failed for ${origin}: ${e.message}`);
        }
      }
      log.info(`  Hydrate: warm-up complete [${activeWarmupOrigins.length}/${(cfg.warmup_origins ?? []).length} origins]`);
    } catch (e) {
      log.warn(`  Hydrate: warm-up page failed: ${e.message}`);
    } finally {
      if (warmupPage) await warmupPage.close().catch(() => {});
    }
  } else {
    // All warmup origins have expired/missing cookies — skip entirely.
    // This prevents context crashes for sessions that need re-authentication.
    // The specific re-auth workflow (google-signin, etc.) will refresh cookies
    // and save them back via saveStorageStateFull(), which merges with any
    // other-domain cookies already in the file.
    log.info(`  Hydrate: warm-up skipped — no valid session cookies for any warmup origin (${(cfg.warmup_origins ?? []).join(', ')})`);
  }

  log.info(`loadAndHydrateContext: COMPLETE for ${sessionId}`);
}


/**
 * Delete the LOCAL session state JSON only.
 *
 * ⚠️  Does NOT touch Google Drive — Drive is the only backup.
 * Drive deletion must be triggered explicitly (e.g. xb_session_delete).
 */
export function deleteStorageState(sessionId) {
  const p = sessionStatePath(sessionId);
  if (fs.existsSync(p)) {
    fs.unlinkSync(p);
    log.info(`Session state deleted locally (Drive copy preserved): ${sessionId}`);
  }
}

/** Remove the local Chrome user-data-dir for a session (ephemeral — not on Drive). */
export function cleanLocalProfile(sessionId) {
  const localPath = localProfilePath(sessionId);
  if (fs.existsSync(localPath)) {
    try {
      fs.rmSync(localPath, { recursive: true, force: true });
      log.info(`Cleaned local Chrome profile: ${localPath}`);
    } catch (e) {
      log.warn(`Could not clean local profile ${localPath}: ${e.message}`);
    }
  }
}

/**
 * Evict stale session cookies — PRESERVE the DB row, the Drive backup, AND the Chrome profile.
 *
 * Serial, tier, exit_node, display_name are durable identity facts.
 * The Drive copy (session JSON + profile tarball) is the ONLY recovery source
 * after a Colab runtime reset — it must NEVER be deleted during eviction.
 *
 * What this does:
 *   1. Deletes LOCAL session JSON only (stale cookies in memory)
 *   2. Resets is_persisted=0 in the DB row (re-persisted after successful re-login)
 *
 * What this does NOT do:
 *   • Does NOT clean local Chrome profile — profile contains multiple domain sessions
 *   • Does NOT call deleteSession()        — DB row is kept intact
 *   • Does NOT delete from Drive           — Drive is the recovery backup
 *   • Does NOT delete the profile tarball  — needed for re-login restore
 */
export function evictSession(sessionId) {
  log.info(`Evicting stale session cookies (Drive backup preserved): ${sessionId}`);

  // Normalise to slug form — DB row is always stored as slug
  const slug = sessionId.includes('@') ? sessionId.replace(/@[^@]+$/, '') : sessionId;

  // 1. Remove LOCAL storageState JSON only — Drive copy is untouched
  deleteStorageState(sessionId);

  // 2. Reset is_persisted=0 so job-manager knows to re-pull on next run
  try {
    setSessionPersisted(slug, false);
    log.info(`Reset is_persisted=0 for session: ${slug}`);
  } catch (e) {
    log.warn(`is_persisted reset failed for ${slug}: ${e.message}`);
  }

  // ✅ Drive copy preserved — ensureSessionState() will re-pull it on next job start
}

/**
 * Surgically evict ONE domain's cookies from the shared state.json file.
 *
 * Unlike evictSession() (which deletes the entire file), this function reads
 * the JSON, strips only the cookies/origins/IndexedDB rows that match the
 * given domain pattern, and writes the file back. All other domain data
 * (e.g. Tailscale, v0, GitHub) is PRESERVED intact.
 *
 * This is the correct eviction primitive for SSO-linked sessions:
 *   - Google expires  → evictDomainFromSession(id, 'google')
 *   - Tailscale expires → evictDomainFromSession(id, 'tailscale')
 *   - v0 expires      → evictDomainFromSession(id, 'v0')
 *
 * is_persisted behaviour:
 *   - Other-domain cookies remain → keep is_persisted=1
 *   - ALL cookies gone            → reset is_persisted=0 (full re-pull needed)
 *
 * @param {string} sessionId      - Session slot ID (email or bare slug)
 * @param {string} domainPattern  - Apex substring to match (case-insensitive contains).
 *                                  Examples: 'google', 'tailscale', 'v0.dev', 'github'
 *                                  Matched against cookie.domain and origin URL strings.
 */
export function evictDomainFromSession(sessionId, domainPattern) {
  const pat = domainPattern.toLowerCase();
  const state = loadStorageState(sessionId);
  if (!state) {
    log.info(`evictDomainFromSession: no state file for ${sessionId} — nothing to evict`);
    return;
  }

  const beforeCount = state.cookies?.length ?? 0;

  // Filter cookies, origins[], and indexedDB[] — keep everything NOT matching the domain
  const filteredCookies = (state.cookies ?? []).filter(
    c => !(c.domain ?? '').toLowerCase().includes(pat)
  );
  const filteredOrigins = (state.origins ?? []).filter(
    o => !o.origin.toLowerCase().includes(pat)
  );
  const filteredIndexedDB = (state.indexedDB ?? []).filter(
    o => !o.origin.toLowerCase().includes(pat)
  );

  const removedCookies  = beforeCount - filteredCookies.length;
  const removedOrigins  = (state.origins?.length ?? 0) - filteredOrigins.length;
  const removedIDB      = (state.indexedDB?.length ?? 0) - filteredIndexedDB.length;

  log.info(
    `evictDomainFromSession: session=${sessionId} domain='${domainPattern}' ` +
    `removed ${removedCookies} cookies, ${removedOrigins} origins, ${removedIDB} IDB entries ` +
    `(${filteredCookies.length} cookies remain)`
  );

  // Write back the pruned state to local file only
  const pruned = {
    ...state,
    cookies:   filteredCookies,
    origins:   filteredOrigins,
    ...(state.indexedDB !== undefined ? { indexedDB: filteredIndexedDB } : {}),
    _domains:  [...new Set(filteredCookies.map(c => c.domain))],
    _evicted_domains: [...(state._evicted_domains ?? []), pat],
    _evicted_at: new Date().toISOString(),
  };

  const p = sessionStatePath(sessionId);
  fs.writeFileSync(p, JSON.stringify(pruned, null, 2));

  // NOTE: intentionally NOT updating is_persisted in the DB.
  // Setting is_persisted=0 would propagate to Supabase via sync and permanently
  // orphan the session cookies stored there. The local eviction is enough for
  // this run — next ensureSessionState() rescue pull will restore from Supabase.
  log.info(
    `evictDomainFromSession: local state pruned for ${sessionId}. ` +
    `is_persisted unchanged — Supabase cookies preserved for rescue.`
  );
}


/**
 * Wipe cookies for a specific domain from the UC/Selenium Chrome profile's SQLite DB.
 *
 * UC/Selenium reuses a persistent profile directory (chrome_profiles/PRFL-XXX_slug/).
 * Stale auth cookies in Default/Cookies can cause the browser to bypass the login
 * form and navigate directly to the authenticated account page, leading to
 * verification_failed errors. This function surgically removes only the affected
 * domain's rows.
 *
 * Generalized version of the google-only wipe that was previously hardcoded in
 * google-signin.mjs — can be called for any domain by any workflow.
 *
 * @param {string} sessionId      - Session slot ID (used to locate the profile dir)
 * @param {string} domainPattern  - SQL LIKE substring, e.g. 'google', 'tailscale', 'v0.dev'
 *                                  Translates to: WHERE host_key LIKE '%{domainPattern}%'
 */
export function evictLocalProfileCookies(sessionId, domainPattern) {
  const profileDir = localProfilePath(sessionId);
  const cookiesDb  = `${profileDir}/Default/Cookies`;

  if (!fs.existsSync(cookiesDb)) {
    log.info(`evictLocalProfileCookies: no Cookies DB at ${cookiesDb} — skipping (fresh profile)`);
    return;
  }

  const tmpScript = `/tmp/xio_clear_cookies_${Date.now()}.py`;
  try {
    fs.writeFileSync(tmpScript, [
      'import sqlite3, sys',
      `conn = sqlite3.connect(${JSON.stringify(cookiesDb)})`,
      'cur = conn.cursor()',
      `cur.execute("DELETE FROM cookies WHERE host_key LIKE '%${domainPattern}%'")`,
      'deleted = cur.rowcount',
      'conn.commit()',
      'conn.close()',
      `print(f'[session-mgr] evictLocalProfileCookies: cleared {deleted} \\'${domainPattern}\\' cookies from UC profile')`,
    ].join('\n'));
    const out = execSync(`python3 ${tmpScript}`, { encoding: 'utf8', timeout: 8000 });
    log.info(out.trim());
  } catch (e) {
    log.warn(`evictLocalProfileCookies: SQLite wipe failed (non-fatal): ${e.message?.slice(0, 120)}`);
  } finally {
    try { fs.unlinkSync(tmpScript); } catch {}
  }
}

// ── Chrome profile (full persistent user-data-dir) ─────────────────────────
// We store a trimmed snapshot on Drive and restore it locally at runtime.
// "Trimmed" = cache, service workers, code cache removed (same as XIO_VERSE pattern).

const TRIM_DIRS = [
  'Default/Cache',
  'Default/Code Cache',
  'Default/Service Worker',
  'Default/GPUCache',
  'Default/Media Cache',
  'ShaderCache',
];

export function localProfilePath(sessionId) {
  const base     = _canonicalBase(sessionId);               // PRFL-021_sunmontueswednesthursfrisatur7
  const canonical = path.join(CHROME_PROFILES_BASE, base);
  if (fs.existsSync(canonical)) return canonical;

  // Check legacy forms and migrate to canonical on first access
  const slug        = sessionId.split('@')[0];
  const underscored = slug.replace(/[^a-zA-Z0-9]/g, '_');
  const legacyForms = [
    path.join(CHROME_PROFILES_BASE, slug),                   // shahid.raiganj
    path.join(CHROME_PROFILES_BASE, underscored),            // shahid_raiganj
    path.join(CHROME_PROFILES_BASE, `${slug}@gmail.com`),   // shahid.raiganj@gmail.com
  ];
  for (const legacy of legacyForms) {
    if (fs.existsSync(legacy)) {
      try {
        fs.renameSync(legacy, canonical);
        log.info(`[session-mgr] Migrated profile: ${path.basename(legacy)} → ${path.basename(canonical)}`);
      } catch (e) {
        log.warn(`[session-mgr] Could not rename ${path.basename(legacy)}: ${e.message} — using legacy`);
        return legacy;
      }
      return canonical;
    }
  }
  return canonical; // new — will be created by browser
}

export function driveProfilePath(sessionId) {
  const base  = _canonicalBase(sessionId);
  const canon = path.join(Paths.chromeProfiles(), `${base}.tar.gz`);
  const legacy = path.join(Paths.chromeProfiles(), `${sessionId}.tar.gz`);
  // Return canonical. If the canonical doesn't exist but legacy does, return legacy (restore phase).
  if (!fs.existsSync(canon) && fs.existsSync(legacy)) return legacy;
  return canon;
}

export function restoreProfile(sessionId) {
  const archivePath = driveProfilePath(sessionId);
  const localPath   = localProfilePath(sessionId);

  // Use node-specific extraction dir when running in multi-runtime environment.
  // This prevents concurrent runtimes from conflicting on the same Chrome profile directory.
  const nodeSlug = process.env.XIO_NODE_NAME?.replace(/[^a-zA-Z0-9-]/g, '-') ?? '';
  const effectiveLocalPath = nodeSlug
    ? path.join(CHROME_PROFILES_BASE, `${_canonicalBase(sessionId)}__${nodeSlug}`)
    : localPath;

  if (!fs.existsSync(archivePath)) {
    log.info(`No local profile archive for ${sessionId} — fresh profile will be created (pull from R2 via ensureSessionState first)`);
    ensureDir(path.dirname(effectiveLocalPath));
    return effectiveLocalPath;
  }

  ensureDir(path.dirname(effectiveLocalPath));
  log.info(`Restoring Chrome profile ${sessionId} from local cache (R2-primary archive)`);
  ensureDir(CHROME_PROFILES_BASE);
  execSync(`tar -xzf "${archivePath}" -C "${path.dirname(effectiveLocalPath)}/"`, { stdio: 'pipe' });

  // Purge stale caches to prevent service worker leaks (same as Colab_B pattern)
  TRIM_DIRS.forEach(rel => {
    const full = path.join(effectiveLocalPath, rel);
    if (fs.existsSync(full)) {
      fs.rmSync(full, { recursive: true, force: true });
    }
  });

  // After extraction, find what dir came out and rename to canonical if needed
  const base = _canonicalBase(sessionId);
  const canonicalPath = path.join(path.dirname(effectiveLocalPath), base);
  if (!fs.existsSync(canonicalPath)) {
    // Tar may contain a slug-named dir — find and rename it
    const slug = sessionId.split('@')[0];
    const underscored = slug.replace(/[^a-zA-Z0-9]/g, '_');
    const candidates = [
      path.join(CHROME_PROFILES_BASE, slug),
      path.join(CHROME_PROFILES_BASE, underscored),
    ];
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) {
        try {
          fs.renameSync(candidate, canonicalPath);
          log.info(`[session-mgr] Renamed extracted dir to canonical: ${base}`);
        } catch (e) {
          log.warn(`[session-mgr] Post-extract rename failed: ${e.message}`);
        }
        break;
      }
    }
  }

  log.info(`Profile restored: ${localPath}`);
  return localPath;
}

export function saveProfile(sessionId) {
  const localPath = localProfilePath(sessionId);

  if (!fs.existsSync(localPath)) {
    log.warn(`Local profile not found, nothing to save for ${sessionId}`);
    return;
  }

  // Trim before saving
  TRIM_DIRS.forEach(rel => {
    const full = path.join(localPath, rel);
    if (fs.existsSync(full)) fs.rmSync(full, { recursive: true, force: true });
  });

  ensureDir(Paths.chromeProfiles());
  const base        = _canonicalBase(sessionId);
  const archivePath = path.join(Paths.chromeProfiles(), `${base}.tar.gz`);
  // Use actual dir basename (may be legacy 'slug@gmail.com' form)
  const dirName     = path.basename(localPath);
  log.info(`Saving Chrome profile ${sessionId} → ${path.basename(archivePath)} (dir: ${dirName})`);
  execSync(`tar -czf "${archivePath}" -C "${CHROME_PROFILES_BASE}/" "${dirName}"`, { stdio: 'pipe' });
  log.info(`Profile saved: ${archivePath}`);
}

/**
 * Push session JSON and/or Chrome profile to all storage tiers.
 *
 * Generalised helper usable by any workflow or the job-manager.
 * Centralises all sync.py push calls with proper timeouts.
 *
 * Storage tiers:
 *   Session JSON    : 1. Supabase (primary, awaited, 15 s)  2. Drive (detached backup)
 *   Chrome profile  : 3. R2      (primary, awaited, 60 s)  4. Drive (detached backup)
 *
 * @param {string}   sessionId
 * @param {object}   [opts]
 * @param {boolean}  [opts.session=true]          - Push session JSON
 * @param {boolean}  [opts.profile=false]         - Push Chrome profile tar.gz
 * @param {boolean}  [opts.awaitPrimary=true]     - Await Supabase + R2; false = all fire-and-forget
 * @param {number}   [opts.sessionTimeoutMs=15000] - Supabase push timeout
 * @param {number}   [opts.profileTimeoutMs=60000] - R2 push timeout
 * @param {Function} [opts.log]                   - Optional log function
 * @returns {Promise<{ session: object|null, profile: object|null }>}
 */
export async function pushToStorage(sessionId, opts = {}) {
  const syncScript = '/content/xio-browser/colab/sync.py';
  if (!fs.existsSync(syncScript)) {
    log.info(`pushToStorage: sync.py not found -- skipping (not on Colab)`);
    return { session: null, profile: null };
  }
  const {
    session          = true,
    profile          = false,
    awaitPrimary     = true,
    sessionTimeoutMs = 15_000,
    profileTimeoutMs = 60_000,
    log: _log,
  } = opts;
  const info = msg => { _log?.(msg); log.info(msg); };

  // Spawn helper: resolves { ok, out } on exit/timeout.
  // detached=true fires-and-forgets (resolves null immediately).
  const _spawn = (args, timeoutMs, detached = false) => new Promise(resolve => {
    import('node:child_process').then(({ spawn: _sp }) => {
      if (detached) {
        try { _sp('python3', args, { detached: true, stdio: 'ignore' }).unref(); } catch {}
        return resolve(null);
      }
      let out = '';
      const child = _sp('python3', args, { stdio: ['ignore', 'pipe', 'pipe'] });
      child.stdout?.on('data', d => { out += d.toString(); });
      child.stderr?.on('data', d => { out += d.toString(); });
      const timer = setTimeout(() => {
        try { child.kill('SIGTERM'); } catch {}
        resolve({ ok: false, out: out.trim() + ' [timeout]' });
      }, timeoutMs);
      child.on('close', code => { clearTimeout(timer); resolve({ ok: code === 0, out: out.trim() }); });
      child.on('error', e  => { clearTimeout(timer); resolve({ ok: false, out: e.message }); });
    }).catch(e => resolve({ ok: false, out: e.message }));
  });

  const slug = sessionId.includes('@') ? sessionId.replace(/@[^@]+$/, '') : sessionId;
  let sessionResult = null;
  let profileResult = null;

  // -- Session JSON -------------------------------------------------------
  if (session) {
    const d1SessionArgs = [syncScript, '--push', '--what', 'sessions', '--target', 'd1', '--session-id', slug];
    const driveSessionArgs = [syncScript, '--push', '--what', 'sessions', '--target', 'drive', '--session-id', slug];
    if (awaitPrimary) {
      sessionResult = await _spawn(d1SessionArgs, sessionTimeoutMs);
      if (sessionResult?.ok) {
        info(`[push] Session JSON -> D1 OK (${slug})`);
      } else {
        info(`[push] Session JSON -> D1 failed (${slug}): ${sessionResult?.out?.slice(-80)}`);
      }
    } else {
      await _spawn(d1SessionArgs, sessionTimeoutMs, true);
    }
    await _spawn(driveSessionArgs, 0, true); // Drive cold backup always detached
    info(`[push] Session JSON -> Drive backup queued (${slug})`);
  }

  // -- Chrome profile tar.gz ---------------------------------------------
  if (profile) {
    try { saveProfile(sessionId); } catch (pe) {
      info(`[push] Local profile tar failed (non-fatal): ${pe.message?.slice(0, 80)}`);
    }
    const r2Args        = [syncScript, '--push', '--what', 'chrome_profiles', '--target', 'r2',    '--session-id', slug];
    const driveProfileArgs = [syncScript, '--push', '--what', 'chrome_profiles', '--target', 'drive', '--session-id', slug];
    if (awaitPrimary) {
      profileResult = await _spawn(r2Args, profileTimeoutMs);
      if (profileResult?.ok) {
        info(`[push] Chrome profile -> R2 OK (${slug})`);
      } else {
        info(`[push] Chrome profile -> R2 failed (${slug}): ${profileResult?.out?.slice(-80)}`);
      }
    } else {
      await _spawn(r2Args, profileTimeoutMs, true);
    }
    await _spawn(driveProfileArgs, 0, true); // Drive cold backup always detached
    info(`[push] Chrome profile -> Drive backup queued (${slug})`);
  }

  return { session: sessionResult, profile: profileResult };
}



/**
 * On-demand sync: Pull session JSON and/or Chrome profile tarball if missing locally.
 * Called before every browser context creation.
 *
 * Storage priority:
 *   - Session JSON    => Supabase (primary, fast REST) => Drive (cold backup fallback)
 *   - Chrome profile  => R2 (primary, fast S3)         => Drive (cold backup fallback)
 *
 * Strategy:
 *   - Always pulls fresh session JSON from Supabase to prevent stale cache
 *   - Only pulls profile if not already present locally
 *   - Uses --session-id for targeted single-session sync
 *   - Normalises sessionId to slug before passing to sync.py
 */
export async function ensureSessionState(sessionId) {
  const syncScript = '/content/xio-browser/colab/sync.py';
  if (!fs.existsSync(syncScript)) {
    log.warn('ensureSessionState: sync.py not found -- skipping on-demand pull');
    return;
  }

  const slug = sessionId.includes('@') ? sessionId.replace(/@[^@]+$/, '') : sessionId;

  // Check what's already present locally
  const sessionJsonExists  = fs.existsSync(sessionStatePath(sessionId));
  const profileTarExists   = fs.existsSync(driveProfilePath(sessionId));
  const profileDirExists   = fs.existsSync(localProfilePath(sessionId));

  // Only pull sessions if D1 doesn't already have this session in memory.
  // (D1 loads all 61 sessions at startup when CF creds are present.)
  // Supabase is deprecated — use D1 as the sessions source.
  const needsSessions = !sessionJsonExists;
  const needsProfiles = !profileTarExists && !profileDirExists;

  if (!needsSessions && !needsProfiles) {
    log.info(`ensureSessionState: all local files present for ${slug} -- no pull needed`);
    return;
  }

  log.info(`ensureSessionState: pulling for ${slug} -- session_json=${needsSessions} (D1), profile=${needsProfiles} (R2)`);


  const runSync = (what, target) => new Promise((resolve) => {
    import('node:child_process').then(({ spawn }) => {
      const args = [syncScript, '--what', what, '--session-id', slug];
      if (target) args.push('--target', target);
      const child = spawn('python3', args, {
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 60000,
      });
      let out = '';
      child.stdout?.on('data', d => { out += d; });
      child.stderr?.on('data', d => { out += d; });
      child.on('close', (code) => {
        if (out.trim()) log.info(`[sync ${what}${target ? '/'+target : ''}] ${out.trim().slice(0, 200)}`);
        resolve(code);
      });
      child.on('error', (e) => {
        log.warn(`ensureSessionState: sync ${what} error: ${e.message}`);
        resolve(1);
      });
    }).catch(() => resolve(1));
  });

  const pulls = [];
  if (needsSessions) pulls.push(runSync('sessions', 'd1'));
  if (needsProfiles) pulls.push(runSync('chrome_profiles', 'r2'));
  await Promise.all(pulls);

  const jsonNow = fs.existsSync(sessionStatePath(sessionId));
  // profile_tar: true if EITHER the archive file OR the extracted directory exists.
  // sync.py extracts the profile to a dir and deletes the archive → dir exists, archive gone.
  const tarNow  = fs.existsSync(driveProfilePath(sessionId)) || fs.existsSync(localProfilePath(sessionId));
  log.info(`ensureSessionState: complete for ${slug} -- session_json=${jsonNow}, profile_tar=${tarNow}`);
}

