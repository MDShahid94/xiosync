/**
 * cookie-health.mjs
 * ─────────────────────────────────────────────────────────────────────────────
 * Analyses Google session cookie files and returns a structured health report.
 *
 * Google uses a layered cookie architecture:
 *   Tier 1 – Primary identity     (SID, __Secure-1PSID …)  ~13 months
 *   Tier 2 – Anti-abuse / CSRF    (SIDCC, PSIDTS, STRP …)  7 days–1 year
 *   Tier 3 – OAuth / service      (OSID, LSID, GAPS …)     ~13 months
 *   Tier 4 – Preferences / GA     (NID, _ga …)             irrelevant to auth
 *
 * Rotating cookies (Tier 2) are auto-renewed by Google's servers on every
 * authenticated page load.  The google-session-refresh workflow triggers that
 * renewal.
 *
 * Refresh thresholds:
 *   CRITICAL  < 7 days   → refresh immediately
 *   WARNING   < 30 days  → schedule refresh soon
 *   OK        ≥ 30 days  → no action needed
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';

// ── Cookie classification ─────────────────────────────────────────────────────

/** All of these must be present for a valid Google session */
export const AUTH_REQUIRED = [
  '__Secure-1PSID', '__Secure-3PSID', 'SID',
  'SSID', 'HSID', 'APISID', 'SAPISID',
  '__Secure-1PAPISID', '__Secure-3PAPISID',
  'LSID',
];

/**
 * Short-lived rotating tokens — renewed automatically by Google's servers
 * on every authenticated page load. These ARE refreshable by navigating
 * to accounts.google.com while logged in.
 */
export const ROTATING = [
  '__Secure-1PSIDTS',    // 1P SID timestamp token     ~1 year   (rotates on use)
  '__Secure-3PSIDTS',    // 3P SID timestamp token     ~1 year   (rotates on use)
  'SIDCC',               // SID cross-check / CSRF     ~1 year
  '__Secure-1PSIDCC',    // Partitioned SIDCC          ~1 year
  '__Secure-3PSIDCC',    // Third-party PSIDCC         ~1 year
  'AEC',                 // Anti-abuse token           ~6 months
];

/**
 * Login-event tokens — only issued during an actual sign-in flow (email +
 * password submission). Cannot be refreshed by a simple page navigation.
 * Their expiry does NOT break an ongoing session — they are anti-replay
 * tokens specific to the authentication event, not to the session itself.
 * We track them separately to avoid false-positive 'immediate' alerts.
 */
export const LOGIN_TOKENS = [
  '__Secure-STRP',       // Secure Token Replay Protection  ~30 days post-login
];

export const CRIT_DAYS = 7;
export const WARN_DAYS = 30;

// ── Per-session health check ──────────────────────────────────────────────────

/**
 * Analyse a single session cookie file.
 * @param   {string} sessionPath  Absolute path to the .json session file
 * @returns {CookieHealthReport}
 */
export function checkCookieHealth(sessionPath) {
  if (!existsSync(sessionPath)) {
    return { ok: false, error: 'session_file_not_found', needs_refresh: false, refresh_urgency: 'none' };
  }

  let cookies;
  try {
    const data = JSON.parse(readFileSync(sessionPath, 'utf8'));
    cookies = data.cookies ?? [];
  } catch (e) {
    return { ok: false, error: `parse_error: ${e.message}`, needs_refresh: false, refresh_urgency: 'none' };
  }

  if (cookies.length === 0) {
    return { ok: false, error: 'no_cookies', needs_refresh: true, refresh_urgency: 'immediate' };
  }

  const nowSec = Date.now() / 1000;

  // Index by name (multiple instances per name for different domains)
  const byName = {};
  for (const c of cookies) {
    (byName[c.name] ??= []).push(c);
  }

  // Missing required auth cookies?
  const missing = AUTH_REQUIRED.filter(n => !byName[n]);

  // Check expiry of all rotating cookies
  const expired  = [];
  const expiring = [];
  for (const name of ROTATING) {
    for (const c of (byName[name] ?? [])) {
      const exp = c.expires ?? -1;
      if (exp <= 0) continue; // session-only — no fixed expiry
      const daysLeft = (exp - nowSec) / 86400;
      const entry = { name, domain: c.domain, days_left: Math.round(daysLeft) };
      if      (daysLeft <= 0)         expired.push(entry);
      else if (daysLeft <= CRIT_DAYS) expiring.push({ ...entry, severity: 'critical' });
      else if (daysLeft <= WARN_DAYS) expiring.push({ ...entry, severity: 'warning' });
    }
  }

  // Login-event tokens: track expiry informally — do NOT affect ok/urgency
  const login_tokens_expired = [];
  for (const name of LOGIN_TOKENS) {
    for (const c of (byName[name] ?? [])) {
      const exp = c.expires ?? -1;
      if (exp > 0 && (exp - nowSec) / 86400 <= 0) {
        login_tokens_expired.push({ name, domain: c.domain, note: 'normal post-login expiry — only renewed by full re-login' });
      }
    }
  }

  // Primary SID lifetime (informational)
  let primary_expires_in_days = null;
  for (const c of [...(byName['__Secure-1PSID'] ?? []), ...(byName['SID'] ?? [])]) {
    if ((c.expires ?? -1) > 0) {
      const d = Math.round((c.expires - nowSec) / 86400);
      if (primary_expires_in_days === null || d < primary_expires_in_days) {
        primary_expires_in_days = d;
      }
    }
  }

  const needs_refresh = missing.length > 0
    || expired.length > 0
    || expiring.some(e => e.severity === 'critical');

  const refresh_urgency =
    (missing.length > 0 || expired.length > 0)  ? 'immediate' :
    expiring.some(e => e.severity === 'critical') ? 'soon'      :
    expiring.length > 0                            ? 'scheduled' : 'none';

  const ok = missing.length === 0 && expired.length === 0 && cookies.length >= 20;

  return {
    ok,
    total_cookies: cookies.length,
    primary_expires_in_days,
    missing_auth_cookies: missing,
    expired,                       // ROTATING cookies past expiry → needs browser refresh
    expiring,                      // ROTATING cookies approaching expiry
    login_tokens_expired,          // LOGIN_TOKENS past expiry → informational only
    needs_refresh,
    refresh_urgency,
  };
}

// ── All-sessions sweep ────────────────────────────────────────────────────────

/**
 * Check every session in a sessions directory.
 * @param   {string} sessionsDir  e.g. /content/xio-mesh/sessions
 * @returns {Array<{ session_id, path, health }>}
 */
export function checkAllSessionsHealth(sessionsDir) {
  if (!existsSync(sessionsDir)) return [];
  try {
    return readdirSync(sessionsDir)
      .filter(f => f.endsWith('.json'))
      .map(f => ({
        session_id: f.slice(0, -5),
        path:       `${sessionsDir}/${f}`,
        health:     checkCookieHealth(`${sessionsDir}/${f}`),
      }));
  } catch {
    return [];
  }
}
