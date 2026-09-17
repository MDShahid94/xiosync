/**
 * google-oauth-loop.mjs
 * ─────────────────────
 * Shared Google OAuth intermediate-page handler for all workflows that
 * authenticate through Google (tailscale-auth, tailscale-signin, google-signin).
 *
 * Handles:
 *   • account chooser (bypass via `continue=` URL param OR data-identifier click)
 *   • challenge/dp  — device push: 30s (3×10s) polling, non-blocking HITL
 *                     notification, then auto-fallback to TOTP
 *   • challenge/totp — TOTP auto-fill from DB totp_secret
 *   • challenge/pwd  — password auto-fill from DB
 *   • challenge/sk / challenge/az / challenge/ipp — screenshot + break
 *   • consent / Allow / Continue screens — auto-click
 *
 * Usage:
 *   import { runGoogleOAuthLoop } from 'file:///content/xio-browser/src/core/google-oauth-loop.mjs';
 *
 *   const result = await runGoogleOAuthLoop(page, {
 *     ctx,          // workflow context — used for ctx.log(), ctx.hitlNotify?.()
 *     shot,         // createShot instance bound to `page`
 *     accountEmail, // full email address (e.g. 'sunmontues...@gmail.com')
 *     totpSecret,   // TOTP secret string (or null if not available)
 *     password,     // account password (or null)
 *     maxRounds,    // max OAuth loop iterations (default 10)
 *     tsUrl,        // optional: TS auth URL to re-navigate to after Google
 *     logPrefix,    // optional: prefix for all log lines (default '[google-oauth]')
 *   });
 *   // result: { ok: boolean, url: string, reason: string }
 */

const _XIOBR = '/content/xio-browser';

/**
 * Non-blocking HITL notification: writes a notice file + logs, does NOT pause flow.
 */
async function _emitNotification(ctx, shot, label, message, stepDir) {
  ctx.log(`\n${'─'.repeat(60)}`);
  ctx.log(`⚠️  2FA NOTICE: ${message}`);
  ctx.log(`${'─'.repeat(60)}\n`);
  // Take a prominent screenshot named after the label
  await shot(label).catch(() => {});
  // Write a JSON notice file (visible in the job steps dir)
  try {
    const { writeFileSync, mkdirSync } = await import('fs');
    const { join } = await import('path');
    if (stepDir) {
      mkdirSync(stepDir, { recursive: true });
      writeFileSync(
        join(stepDir, `2fa_notice_${label}.json`),
        JSON.stringify({ label, message, ts: new Date().toISOString() }, null, 2)
      );
    }
  } catch (_) {}
}

/**
 * Handles the Google account chooser page.
 * Strategy A: extract `continue=` URL param and navigate directly (bypasses UI).
 * Strategy B: click account row by data-identifier / getByText (fallback).
 * Returns true if we navigated away from the chooser.
 */
async function _handleAccountChooser(page, { ctx, shot, accountEmail, logPfx = '[google-oauth]' }) {
  const url = page.url();
  if (!url.includes('accountchooser') && !url.includes('accounts.google.com')) return false;

  ctx.log(`${logPfx} Account chooser detected — bypassing…`);
  await shot('chooser_detected').catch(() => {});

  // ── Strategy A: navigate to continue= URL ──────────────────────────────
  try {
    const _chooserUrl = new URL(url);
    const _cont = _chooserUrl.searchParams.get('continue');
    if (_cont) {
      const _continueUrl = decodeURIComponent(_cont);
      ctx.log(`${logPfx} [chooser-A] goto continue URL: ${_continueUrl.slice(0, 100)}`);
      await page.goto(_continueUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
      await page.waitForTimeout(2000);
      await shot('chooser_continue_url').catch(() => {});
      ctx.log(`${logPfx} [chooser-A] post-continue URL: ${page.url().slice(0, 100)}`);
      if (!page.url().includes('accountchooser')) return true;
    }
  } catch (_cu) {
    ctx.log(`${logPfx} [chooser-A] continue-URL failed: ${_cu.message?.slice(0, 60)}`);
  }

  // ── Strategy B: click account row ──────────────────────────────────────
  ctx.log(`${logPfx} [chooser-B] clicking account row for ${accountEmail}…`);
  const _acctBase = accountEmail.replace('@gmail.com', '');
  const clicked =
    await page.locator(`[data-identifier="${accountEmail}"]`).first().click({ timeout: 4000 }).then(() => true).catch(() => false) ||
    await page.locator(`[data-identifier="${_acctBase}"]`).first().click({ timeout: 3000 }).then(() => true).catch(() => false) ||
    await page.getByText(accountEmail, { exact: true }).first().click({ timeout: 4000 }).then(() => true).catch(() => false) ||
    await page.getByText(_acctBase, { exact: false }).first().click({ timeout: 3000 }).then(() => true).catch(() => false) ||
    await page.locator(`[data-email="${accountEmail}"]`).first().click({ timeout: 3000 }).then(() => true).catch(() => false) ||
    await page.locator('li[data-identifier], [role="link"][data-authuser]').first().click({ timeout: 3000 }).then(() => true).catch(() => false);

  ctx.log(`${logPfx} [chooser-B] click result: ${clicked}`);
  await page.waitForTimeout(2500);
  await shot('chooser_after_click').catch(() => {});
  ctx.log(`${logPfx} [chooser-B] post-click URL: ${page.url().slice(0, 100)}`);
  return clicked;
}

/**
 * Handles challenge/dp (device push) with:
 * - Non-blocking HITL notification (screenshot + log + JSON file)
 * - 30s (3×10s) polling for auto-approval
 * - If not approved: click "More ways to verify" → select TOTP option
 * Returns 'approved' | 'switched_to_totp' | 'stuck'
 */
async function _handleDevicePush(page, { ctx, shot, accountEmail, stepDir, logPfx = '[google-oauth]' }) {
  ctx.log(`${logPfx} [dp] Device push challenge — sending non-blocking notification…`);

  // Non-blocking: take screenshot + emit notice (does NOT pause the job)
  await _emitNotification(
    ctx, shot,
    'dp_2fa_sent',
    `Device push sent to phone for ${accountEmail}. ` +
    `Flow will auto-continue after 30s (TOTP fallback if not approved).`,
    stepDir
  );

  // ── Poll 3×10s for auto-approval ───────────────────────────────────────
  let _dpResolved = false;
  for (let _p = 1; _p <= 3; _p++) {
    ctx.log(`${logPfx} [dp] Poll ${_p}/3 — waiting 10s for device tap…`);
    await page.waitForTimeout(10_000);
    await shot(`dp_poll_${_p}`).catch(() => {});
    if (!page.url().includes('challenge/dp')) {
      ctx.log(`${logPfx} [dp] ✅ Poll ${_p}/3 — device approved! URL: ${page.url().slice(0, 80)}`);
      _dpResolved = true;
      break;
    }
    ctx.log(`${logPfx} [dp] Poll ${_p}/3 — still on dp challenge`);
  }

  if (_dpResolved) return 'approved';

  // ── 30s expired — switch to TOTP ───────────────────────────────────────
  ctx.log(`${logPfx} [dp] Device not approved after 30s — switching to TOTP via "More ways to verify"…`);
  await shot('dp_more_ways_attempt').catch(() => {});

  const _moreBtn = await page.waitForSelector(
    'a:has-text("More ways to verify"), [role="link"]:has-text("More ways to verify"), ' +
    'button:has-text("More ways to verify"), a:has-text("Try another way"), ' +
    'button:has-text("Try another way"), [role="link"]:has-text("Try another way")',
    { timeout: 8_000, state: 'visible' }
  ).catch(() => null);

  if (_moreBtn) {
    await _moreBtn.click();
    ctx.log(`${logPfx} [dp] Clicked "More ways to verify" — waiting for options…`);
    await page.waitForTimeout(3000);
    await shot('dp_more_ways_options').catch(() => {});

    // Select Authenticator app option
    const _authBtn = await page.waitForSelector(
      '[data-challengeType="6"], [data-challengeType="totp"], ' +
      'li:has-text("Authenticator"), [aria-label*="authenticator" i], ' +
      'li:has-text("Google Authenticator")',
      { timeout: 5_000, state: 'visible' }
    ).catch(() => null);

    if (_authBtn) {
      await _authBtn.click();
      ctx.log(`${logPfx} [dp] Selected Authenticator option`);
      await page.waitForTimeout(2000);
    } else {
      ctx.log(`${logPfx} [dp] No authenticator option — TOTP input may already be visible`);
    }
    return 'switched_to_totp';
  }

  ctx.log(`${logPfx} [dp] "More ways to verify" not found — cannot auto-resolve`);
  return 'stuck';
}

/**
 * Handles challenge/totp — auto-fills from totpSecret using generateTOTP.
 * Returns true on success.
 */
async function _handleTotp(page, { ctx, shot, totpSecret, logPfx = '[google-oauth]' }) {
  ctx.log(`${logPfx} [totp] TOTP challenge — auto-filling…`);
  if (!totpSecret) {
    ctx.log(`${logPfx} [totp] No totp_secret available — cannot auto-fill`);
    return false;
  }
  try {
    const { generateTOTP } = await import(`file://${_XIOBR}/src/core/stealth-runner.mjs`);
    const code = generateTOTP(totpSecret);
    ctx.log(`${logPfx} [totp] Generated code: ${code}`);

    const totpEl = await page.waitForSelector(
      'input[type="tel"], input[type="number"], input[autocomplete="one-time-code"], input[inputmode="numeric"]',
      { timeout: 8_000, state: 'visible' }
    ).catch(() => null);

    if (!totpEl) {
      ctx.log(`${logPfx} [totp] No TOTP input found`);
      return false;
    }

    await totpEl.fill('');
    await page.keyboard.type(code, { delay: 80 });
    await page.waitForTimeout(500);

    const nextBtn = await page.waitForSelector(
      '[id="totpNext"], button:has-text("Next"), button:has-text("Verify"), input[type="submit"]',
      { timeout: 5000, state: 'visible' }
    ).catch(() => null);
    if (nextBtn) await nextBtn.click();

    await page.waitForTimeout(3500);
    await shot('totp_submitted').catch(() => {});
    ctx.log(`${logPfx} [totp] TOTP submitted — URL: ${page.url().slice(0, 80)}`);
    return true;
  } catch (_te) {
    ctx.log(`${logPfx} [totp] Error: ${_te.message}`);
    return false;
  }
}

/**
 * Handles challenge/pwd — auto-fills password from `password` param.
 * Returns true on success.
 */
async function _handlePassword(page, { ctx, shot, password, logPfx = '[google-oauth]' }) {
  ctx.log(`${logPfx} [pwd] Password challenge — auto-filling…`);
  if (!password) {
    ctx.log(`${logPfx} [pwd] No password available — cannot auto-fill`);
    return false;
  }
  try {
    const pwdEl = await page.waitForSelector(
      "input[type='password'], input[name='password']",
      { timeout: 8000, state: 'visible' }
    ).catch(() => null);

    if (!pwdEl) {
      ctx.log(`${logPfx} [pwd] No password input found`);
      return false;
    }

    await pwdEl.fill(password);
    await page.waitForTimeout(500);

    const nxtBtn = await page.waitForSelector(
      '[id="passwordNext"], button:has-text("Next"), input[type="submit"]',
      { timeout: 5000, state: 'visible' }
    ).catch(() => null);
    if (nxtBtn) await nxtBtn.click();

    await page.waitForTimeout(3500);
    await shot('pwd_submitted').catch(() => {});
    ctx.log(`${logPfx} [pwd] Password submitted — URL: ${page.url().slice(0, 80)}`);
    return true;
  } catch (_pe) {
    ctx.log(`${logPfx} [pwd] Error: ${_pe.message}`);
    return false;
  }
}

/**
 * Main export: runGoogleOAuthLoop
 * Iterates through Google OAuth intermediate pages until reaching a non-Google URL
 * or maxRounds is exhausted.
 *
 * @param {import('patchright').Page} page
 * @param {Object} opts
 * @param {Object}   opts.ctx           - workflow context (ctx.log, ctx.hitlNotify)
 * @param {Function} opts.shot          - screenshot function bound to page
 * @param {string}   opts.accountEmail  - full email (e.g. 'foo@gmail.com')
 * @param {string}   [opts.totpSecret]  - TOTP secret string or null
 * @param {string}   [opts.password]    - account password or null
 * @param {number}   [opts.maxRounds]   - max iterations (default 10)
 * @param {string}   [opts.stepDir]     - path to write 2fa_notice_*.json files
 * @param {string}   [opts.logPrefix]   - log prefix (default '[google-oauth]')
 * @param {string}   [opts.successUrl]  - URL fragment that signals success (e.g. 'login.tailscale.com')
 * @returns {Promise<{ok: boolean, url: string, reason: string}>}
 */
export async function runGoogleOAuthLoop(page, opts = {}) {
  const {
    ctx,
    shot,
    accountEmail = '',
    totpSecret   = null,
    password     = null,
    maxRounds    = 10,
    stepDir      = null,
    logPrefix    = '[google-oauth]',
    successUrl   = null,
  } = opts;

  const log = (msg) => ctx?.log ? ctx.log(msg) : console.log(msg);

  for (let round = 0; round < maxRounds; round++) {
    const url = page.url();
    log(`${logPrefix} Round ${round + 1}/${maxRounds}: ${url.slice(0, 100)}`);

    // ── Done: left Google entirely ────────────────────────────────────────
    if (!url.includes('accounts.google.com') && !url.includes('accountchooser')) {
      // Also check for success URL if specified
      if (!successUrl || url.includes(successUrl)) {
        log(`${logPrefix} ✅ Left Google — URL: ${url.slice(0, 100)}`);
        return { ok: true, url, reason: 'left_google' };
      }
    }

    // ── Account chooser ───────────────────────────────────────────────────
    if (url.includes('accountchooser') || url.includes('account-chooser')) {
      await _handleAccountChooser(page, { ctx, shot, accountEmail, logPfx: logPrefix });
      continue;
    }

    // ── Device push (challenge/dp) ─────────────────────────────────────────
    if (url.includes('challenge/dp')) {
      const dpResult = await _handleDevicePush(page, { ctx, shot, accountEmail, stepDir, logPfx: logPrefix });
      if (dpResult === 'stuck') {
        log(`${logPrefix} [dp] Stuck — cannot proceed`);
        return { ok: false, url: page.url(), reason: 'dp_stuck' };
      }
      continue; // re-evaluate URL on next round
    }

    // ── TOTP challenge ─────────────────────────────────────────────────────
    if (url.includes('challenge/totp') || url.includes('challenge/sk')) {
      const ok = await _handleTotp(page, { ctx, shot, totpSecret, logPfx: logPrefix });
      if (!ok) return { ok: false, url: page.url(), reason: 'totp_failed' };
      continue;
    }

    // ── Password challenge ─────────────────────────────────────────────────
    if (url.includes('challenge/pwd') || url.includes('signin/v2/challenge/pwd')) {
      const ok = await _handlePassword(page, { ctx, shot, password, logPfx: logPrefix });
      if (!ok) return { ok: false, url: page.url(), reason: 'pwd_failed' };
      continue;
    }

    // ── Unsupported challenge (ipp, az, etc.) ──────────────────────────────
    if (/\/challenge\/(ipp|az|ipp|pwa)/.test(url)) {
      log(`${logPrefix} Unsupported challenge type at ${url.slice(0, 80)} — breaking`);
      await shot('unsupported_challenge').catch(() => {});
      return { ok: false, url, reason: `unsupported_challenge` };
    }

    // ── Consent / Allow / Continue screens ────────────────────────────────
    const btn = await page.waitForSelector(
      'button:has-text("Continue"), [role="button"]:has-text("Continue"), ' +
      'button:has-text("Allow"), #submit_approve_access, ' +
      'button:has-text("Next"), input[type="submit"][value="Allow"], ' +
      '[data-value="Allow"], input[type="submit"]',
      { timeout: 6000, state: 'visible' }
    ).catch(() => null);

    if (btn) {
      const btnText = await btn.evaluate(el => el.textContent?.trim() || el.value?.trim() || '?').catch(() => '?');
      log(`${logPrefix} Clicking consent button: "${btnText}"`);
      await shot(`consent_${round}`).catch(() => {});
      await btn.click();
      await page.waitForTimeout(3000);
    } else {
      log(`${logPrefix} No button found at ${url.slice(0, 80)} — waiting 3s`);
      await page.waitForTimeout(3000);
    }
  }

  const finalUrl = page.url();
  log(`${logPrefix} maxRounds (${maxRounds}) reached — final URL: ${finalUrl.slice(0, 100)}`);
  return { ok: !finalUrl.includes('accounts.google.com'), url: finalUrl, reason: 'max_rounds' };
}
