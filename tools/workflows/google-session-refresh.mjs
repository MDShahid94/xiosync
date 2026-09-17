/**
 * google-session-refresh.mjs
 * ─────────────────────────────────────────────────────────────────────────────
 * Refreshes a Google session's short-lived rotating cookies without re-login.
 *
 * HOW IT WORKS
 * ────────────
 * Google's servers auto-rotate Tier-2 cookies (STRP, PSIDTS, SIDCC …) on every
 * authenticated page load.  This workflow:
 *   1. Reads existing cookies from the session JSON (Supabase-primary, Drive-fallback)
 *   2. Loads them into a patchright context (making the browser "logged in")
 *   3. Navigates to accounts.google.com — Google responds with Set-Cookie headers
 *      containing fresh STRP, PSIDTS, SIDCC etc.
 *   4. Extracts ALL updated cookies via CDP Network.getAllCookies
 *   5. Merges them back into the session file (preserving any cookies not in the
 *      response — e.g. service-specific ones from gmail.google.com)
 *   6. Saves the updated session to disk (caller pushes to Supabase/Drive)
 *
 * WHEN TO RUN
 * ───────────
 * The keep-alive loop checks every 2 hours and auto-queues this workflow when:
 *   • __Secure-STRP     < 7 days  remaining  (expires fastest — ~30 days)
 *   • __Secure-*PSIDTS  < 7 days  remaining
 *   • SIDCC             < 7 days  remaining
 *   • Any AUTH_REQUIRED cookie missing
 *
 * Can also be triggered manually:
 *   xb_run_workflow(workflow="google-session-refresh", session_id="my_account")
 *
 * PARAMS (all optional)
 * ─────────────────────
 *   force   {boolean}  Refresh even if health check says OK  (default: false)
 */

export const meta = {
  name:        'google-session-refresh',
  description: 'Refreshes short-lived Google session cookies (STRP, PSIDTS, SIDCC) without re-login. Run when xb_session_health reports urgency=soon or immediate. Requires an existing valid session.',
  requires:    ['google_session'],
  params: {
    session_id: 'Slot ID whose session.json to refresh (required)',
  },
};


import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createLogger } from '../src/utils/logger.mjs';

const log = createLogger('session-refresh');

import { fileURLToPath } from 'node:url';
import { resolve as _resolve, dirname as _dirname } from 'node:path';

const _thisDir = _dirname(fileURLToPath(import.meta.url));
function _findXiobrRoot(fromDir) {
  const c1 = _resolve(fromDir, '..', 'src', 'utils', 'cookie-health.mjs');
  if (existsSync(c1)) return _resolve(fromDir, '..');
  const c2 = _resolve(fromDir, '..', '..', 'src', 'utils', 'cookie-health.mjs');
  if (existsSync(c2)) return _resolve(fromDir, '../..');
  return '/content/xio-browser';
}
const _xiobr = _findXiobrRoot(_thisDir);

// Lazy-load cookie-health and devtools-manager
let _checkHealth, _cdpGetAllCookies;
try {
  const ch = await import(`file://${_xiobr}/src/utils/cookie-health.mjs`);
  _checkHealth = ch.checkCookieHealth;
} catch {
  _checkHealth = () => ({ ok: false, needs_refresh: true, refresh_urgency: 'immediate' });
}
try {
  const dm = await import(`file://${_xiobr}/src/core/devtools-manager.mjs`);
  _cdpGetAllCookies = dm.cdpGetAllCookies;
} catch {
  _cdpGetAllCookies = null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── Main export ───────────────────────────────────────────────────────────────

export async function run(ctx, params = {}) {
  const { force = false } = params;
  const page      = ctx.page;
  const sessionId = ctx.sessionId ?? 'unknown';
  const sessionPath = `/content/xio-mesh/sessions/${sessionId}.json`;
  const _log = ctx.log?.bind(ctx) ?? log.info.bind(log);

  // ── Phase 0: Health check — is a refresh actually needed? ─────────────────
  await ctx.step('phase0_health_check', async () => {
    _log('[refresh] Checking cookie health...');
    const health = _checkHealth(sessionPath);
    _log(`[refresh] Health: ok=${health.ok} urgency=${health.refresh_urgency} ` +
         `expired=${health.expired?.length ?? 0} expiring=${health.expiring?.length ?? 0}`);

    if (health.error) {
      throw new Error(`Session file problem: ${health.error}`);
    }

    if (health.ok && !force) {
      _log('[refresh] ✅ All cookies healthy — skipping refresh (pass force=true to override)');
      ctx.setResult?.({
        skipped:   true,
        reason:    'all_cookies_healthy',
        health,
        session_id: sessionId,
      });
      // Signal early-exit via a sentinel result; step machinery handles the screenshot
      return;
    }

    if (health.needs_refresh) {
      _log(`[refresh] ⚠️  Refresh needed (urgency: ${health.refresh_urgency})`);
      if (health.expired?.length)   _log(`[refresh]   Expired: ${health.expired.map(e => e.name).join(', ')}`);
      if (health.expiring?.length)  _log(`[refresh]   Expiring: ${health.expiring.map(e => `${e.name}(${e.days_left}d)`).join(', ')}`);
      if (health.missing_auth_cookies?.length) _log(`[refresh]   Missing: ${health.missing_auth_cookies.join(', ')}`);
    }
  });

  // Check if early exit was set
  // (ctx.result is set inside the step — peek at it)
  if (ctx._earlyResult?.skipped) {
    return ctx._earlyResult;
  }

  // ── Phase 1: Load existing cookies into patchright context ────────────────
  let beforeCount = 0;
  await ctx.step('phase1_load_session', async () => {
    _log('[refresh] Loading existing session cookies into browser...');
    if (!existsSync(sessionPath)) {
      throw new Error(`Session file not found: ${sessionPath}`);
    }
    const data = JSON.parse(readFileSync(sessionPath, 'utf8'));
    const cookies = data.cookies ?? [];
    if (cookies.length === 0) throw new Error('Session has no cookies — cannot refresh, must re-login');
    beforeCount = cookies.length;
    await page.context().clearCookies();
    await page.context().addCookies(cookies);
    _log(`[refresh] Loaded ${cookies.length} cookies into context`);
  });

  // ── Phase 2: Navigate to Google — triggers automatic cookie rotation ───────
  await ctx.step('phase2_trigger_rotation', async () => {
    _log('[refresh] Navigating to accounts.google.com to trigger cookie rotation...');
    await page.goto('https://accounts.google.com/', {
      waitUntil: 'networkidle', timeout: 20000,
    }).catch(() => page.goto('https://accounts.google.com/', {
      waitUntil: 'domcontentloaded', timeout: 15000,
    }));
    // Wait for Google to issue Set-Cookie responses
    await sleep(2500);

    const url = page.url();
    _log(`[refresh] Landed at: ${url.slice(0, 80)}`);
    if (url.includes('signin') || url.includes('ServiceLogin')) {
      throw new Error('Redirected to sign-in page — session has expired, need full re-login');
    }
    _log('[refresh] ✅ Still logged in — Google has rotated the short-lived tokens');
  });

  // ── Phase 3: Extract ALL fresh cookies via CDP ────────────────────────────
  let freshCookies = [];
  await ctx.step('phase3_extract_cookies', async () => {
    _log('[refresh] Extracting fresh cookies via CDP Network.getAllCookies...');

    if (_cdpGetAllCookies) {
      // Use devtools-manager helper (gets ALL cookies, cross-domain)
      freshCookies = await _cdpGetAllCookies(page.context(), page);
    } else {
      // Fallback: use Playwright's context API across all known Google domains
      freshCookies = await page.context().cookies([
        'https://accounts.google.com',
        'https://google.com',
        'https://www.google.com',
        'https://mail.google.com',
        'https://myaccount.google.com',
        'https://youtube.com',
        'https://www.youtube.com',
        'https://google.co.in',
      ]);
    }
    _log(`[refresh] Got ${freshCookies.length} cookies from browser`);

    // Merge: fresh cookies override, preserve old ones not present in fresh set
    const oldData = JSON.parse(readFileSync(sessionPath, 'utf8'));
    const oldCookies = oldData.cookies ?? [];
    const freshByKey = new Map(freshCookies.map(c => [`${c.name}@${c.domain}`, c]));
    const merged = [
      ...freshCookies,
      ...oldCookies.filter(c => !freshByKey.has(`${c.name}@${c.domain}`)),
    ];

    _log(`[refresh] Merged: ${freshCookies.length} fresh + ${merged.length - freshCookies.length} preserved = ${merged.length} total`);
    freshCookies = merged;
  });

  // ── Phase 4: Write back (caller handles push to Supabase/R2/Drive) ────────
  await ctx.step('phase4_save_session', async () => {
    _log('[refresh] Writing refreshed session to disk...');
    const payload = { cookies: freshCookies, refreshed_at: new Date().toISOString() };
    writeFileSync(sessionPath, JSON.stringify(payload, null, 2));
    _log(`[refresh] Saved ${freshCookies.length} cookies to ${sessionPath}`);
  });

  // ── Phase 5: Final health check — confirm rotation worked ─────────────────
  let afterHealth;
  await ctx.step('phase5_verify_health', async () => {
    afterHealth = _checkHealth(sessionPath);
    if (afterHealth.needs_refresh && afterHealth.refresh_urgency === 'immediate') {
      _log(`[refresh] ⚠️  Still critical after refresh — missing: ${afterHealth.missing_auth_cookies?.join(', ')}`);
    } else {
      _log(`[refresh] ✅ Health after refresh: urgency=${afterHealth.refresh_urgency} primary_expires_in=${afterHealth.primary_expires_in_days}d`);
    }
  });

  const result = {
    success:         true,
    session_id:      sessionId,
    cookies_before:  beforeCount,
    cookies_after:   freshCookies.length,
    health_after:    afterHealth,
  };
  ctx.setResult?.(result);
  return result;
}
