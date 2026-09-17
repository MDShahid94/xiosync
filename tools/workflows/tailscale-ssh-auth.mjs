/**
 * tailscale-ssh-auth.mjs
 * ─────────────────────────────────────────────────────────────────────────────
 * Auto-authenticates a Tailscale SSH "additional check" URL using the persisted
 * Google session of the mesh-admin account (samnurnihartalukdar@gmail.com).
 *
 * BACKGROUND
 * ──────────
 * When a Colab runtime connects via Tailscale SSH for the first time (or after
 * a key rotation), Tailscale issues a per-session check:
 *   "Tailscale SSH requires an additional check."
 *   "To authenticate, visit: https://login.tailscale.com/a/XXXXXXXX"
 *
 * That URL is a Tailscale Admin Console page that authenticates the user via
 * Google OAuth (since the mesh admin account is a Google account). If we have
 * the admin's Google cookies loaded, the page auto-completes — no human click.
 *
 * WORKFLOW
 * ────────
 *   phase0 — Check persisted session health for mesh admin account
 *   phase1 — Load Google cookies into patchright context
 *   phase2 — Navigate to https://login.tailscale.com/a/<token>
 *   phase3 — Detect completion (page shows "Connection authorized" or similar)
 *   phase4 — Screenshot final state + report result
 *
 * FALLBACK
 * ────────
 * If no persisted session exists (or is expired), the workflow outputs the
 * raw auth URL for manual authentication and exits cleanly with ok=false.
 *
 * PARAMS
 * ──────
 *   auth_url   {string}  Required. The full https://login.tailscale.com/a/... URL
 *   session_id {string}  Optional. Default: "mesh_admin_samnur"
 *
 * CALLED BY
 * ─────────
 *   boot.py keep-alive: when 'tailscale ssh' or node-check outputs an auth URL
 *   Manually:  xb_run_workflow(workflow="tailscale-ssh-auth", auth_url="https://login.tailscale.com/a/...")
 */

export const meta = {
  name:        'tailscale-ssh-auth',
  description: 'Authenticates the Colab worker to the Tailscale network using a pre-generated auth key. Establishes the Tailscale tunnel so the worker is reachable from Mac and other nodes.',
  requires:    [],
  params: {
    auth_key: 'Tailscale auth key (tskey-auth-...)',
  },
};


import { readFileSync, existsSync } from 'node:fs';
import { createLogger } from '../src/utils/logger.mjs';

const log = createLogger('ts-auth');

import { fileURLToPath } from 'node:url';
import { resolve as _resolve, dirname as _dirname } from 'node:path';

const _thisDir = _dirname(fileURLToPath(import.meta.url));
const _xiobr   = existsSync(`${_thisDir}/../src/utils/cookie-health.mjs`)
  ? _resolve(_thisDir, '..')
  : '/content/xio-browser';

// Lazy-load cookie-health
let _checkHealth;
try {
  const ch = await import(`file://${_xiobr}/src/utils/cookie-health.mjs`);
  _checkHealth = ch.checkCookieHealth;
} catch {
  _checkHealth = () => ({ ok: false, needs_refresh: false, refresh_urgency: 'none' });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Completion signals Tailscale shows after auth succeeds
const SUCCESS_SIGNALS = [
  'authorized',
  'success',
  'approved',
  'you may now close',
  'authentication complete',
  'connection approved',
];

export async function run(ctx, params = {}) {
  const { auth_url, session_id = 'mesh_admin_samnur' } = params;
  const page        = ctx.page;
  const sessionPath = `/content/xio-mesh/sessions/${session_id}.json`;
  const _log        = ctx.log?.bind(ctx) ?? log.info.bind(log);

  if (!auth_url) {
    throw new Error('auth_url param is required (e.g. https://login.tailscale.com/a/...)');
  }

  // ── Phase 0: Check session health + fallback decision ─────────────────────
  let useSavedSession = false;
  await ctx.step('phase0_session_check', async () => {
    _log(`[ts-auth] Checking session health for: ${session_id}`);
    if (!existsSync(sessionPath)) {
      _log(`[ts-auth] ⚠️  No persisted session for ${session_id}`);
      _log(`[ts-auth] 🔗 Manual auth required: ${auth_url}`);
      ctx.setResult?.({ ok: false, fallback: true, manual_url: auth_url, session_id });
      return;
    }
    const health = _checkHealth(sessionPath);
    _log(`[ts-auth] Session health: ok=${health.ok} urgency=${health.refresh_urgency}`);
    if (!health.ok && health.refresh_urgency === 'immediate') {
      _log(`[ts-auth] ⚠️  Session critically degraded — falling back to manual auth`);
      _log(`[ts-auth] 🔗 Manual auth URL: ${auth_url}`);
      ctx.setResult?.({ ok: false, fallback: true, manual_url: auth_url, session_id, health });
      return;
    }
    useSavedSession = true;
    _log(`[ts-auth] ✅ Session OK — proceeding with auto-auth`);
  });

  if (!useSavedSession) return ctx._earlyResult ?? { ok: false, fallback: true, manual_url: auth_url };

  // ── Phase 1: Load mesh-admin Google cookies ────────────────────────────────
  await ctx.step('phase1_load_session', async () => {
    _log(`[ts-auth] Loading ${session_id} cookies into browser...`);
    const { readFileSync } = await import('node:fs');
    const data    = JSON.parse(readFileSync(sessionPath, 'utf8'));
    const cookies = data.cookies ?? [];
    if (cookies.length === 0) throw new Error('Session file has no cookies');
    await page.context().clearCookies();
    await page.context().addCookies(cookies);
    _log(`[ts-auth] Loaded ${cookies.length} Google cookies`);
  });

  // ── Phase 2: Navigate to Tailscale auth URL ────────────────────────────────
  await ctx.step('phase2_navigate_auth_url', async () => {
    _log(`[ts-auth] Navigating to: ${auth_url}`);
    await page.goto(auth_url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await sleep(2000);

    const url = page.url();
    _log(`[ts-auth] Landed at: ${url.slice(0, 80)}`);

    // If redirected to Google login — our cookies weren't accepted
    if (url.includes('accounts.google.com') && url.includes('signin')) {
      throw new Error('Google session not accepted — cookies may be expired. Run google-session-refresh first.');
    }
  });

  // ── Phase 3: Detect and handle completion ─────────────────────────────────
  let authSuccess = false;
  let finalUrl    = '';
  await ctx.step('phase3_detect_completion', async () => {
    // Tailscale auth pages sometimes require a button click to confirm
    await sleep(1500);

    // Try clicking any "Approve" / "Authorize" / "Allow" button
    const btnSelectors = [
      'button:has-text("Approve")',
      'button:has-text("Authorize")',
      'button:has-text("Allow")',
      'button:has-text("Continue")',
      'button[type="submit"]',
      'input[type="submit"]',
    ];
    for (const sel of btnSelectors) {
      try {
        const btn = page.locator(sel).first();
        if (await btn.isVisible({ timeout: 1500 })) {
          _log(`[ts-auth] Clicking auth button: ${sel}`);
          await btn.click();
          await sleep(2000);
          break;
        }
      } catch { /* selector not found — continue */ }
    }

    await sleep(2000);
    finalUrl = page.url();
    const bodyText = (await page.textContent('body').catch(() => '')).toLowerCase();
    authSuccess = SUCCESS_SIGNALS.some(sig => bodyText.includes(sig))
      || finalUrl.includes('success')
      || finalUrl.includes('authorized')
      // Tailscale sometimes just redirects back to admin console on success
      || finalUrl.includes('login.tailscale.com/admin')
      || finalUrl.includes('tailscale.com/machines');

    _log(`[ts-auth] Final URL: ${finalUrl.slice(0, 80)}`);
    _log(`[ts-auth] Auth success detected: ${authSuccess}`);
  });

  // ── Phase 4: Report result ─────────────────────────────────────────────────
  await ctx.step('phase4_result', async () => {
    if (authSuccess) {
      _log(`[ts-auth] ✅ Tailscale SSH check AUTHORIZED via ${session_id}`);
    } else {
      _log(`[ts-auth] ⚠️  Could not auto-detect completion — check screenshot`);
      _log(`[ts-auth] 🔗 If auth failed, visit manually: ${auth_url}`);
    }
  });

  const result = { ok: authSuccess, fallback: false, session_id, final_url: finalUrl, auth_url };
  ctx.setResult?.(result);
  return result;
}
