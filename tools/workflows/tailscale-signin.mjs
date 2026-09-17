/**
 * tailscale-signin.mjs — Standalone Tailscale sign-in workflow
 *
 * Uses the mesh-admin profile (PRFL-021: sunmontueswednesthursfrisatur7@gmail.com)
 * to sign into Tailscale via Google OAuth.
 *
 * Flow:
 *   Phase 0: ensure_google_session — run google-signin for mesh-admin to get
 *            a fresh Google session (auto-skipped if session still valid)
 *   Phase 1: prepare_mesh_admin_profile — pull profile from Drive
 *   Phase 2: tailscale_signin — navigate to login.tailscale.com, sign in via
 *            Google (auto-skipped if already signed in)
 *
 * Run standalone:
 *   xb_run_workflow({ workflow: 'tailscale-signin',
 *                     session_id: 'sunmontueswednesthursfrisatur7@gmail.com' })
 *
 * Also called as a sub-workflow from self-spawn (Phase 5) before tailscale-auth.
 */

// Mesh-admin is resolved from ecosystem.mjs (config-driven via xio_config.json → networks)
const MESH_ADMIN_SESSION = getMeshAdmin('tailnet-primary');           // bare session_id
const MESH_ADMIN_EMAIL   = getMeshAdminEmail('tailnet-primary');      // full email for Google chooser
const TS_LOGIN_URL       = 'https://login.tailscale.com/login';
const TS_CONSOLE_PATTERN = 'console.tailscale.com';

import { attachTestRunner } from './_test-runner.mjs';
import path from 'node:path';
import { createShot } from '../src/core/wf-shot.mjs';
import { getMeshAdmin, getMeshAdminEmail } from '../src/core/ecosystem.mjs';
import { runGoogleOAuthLoop } from '../src/core/google-oauth-loop.mjs';




export async function run(ctx, params) {
  attachTestRunner(ctx, import.meta.url);
  // jobDir: use ctx.jobDir (full absolute path) if available, fall back gracefully
  const _jobDir = ctx.jobDir
    ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : `/tmp/ts-signin-${Date.now()}`);
  const _evDir = `${_jobDir}/steps`;
  const { mkdirSync } = await import('node:fs');
  mkdirSync(_evDir, { recursive: true });

  let meshAdminProfileDir = null;

  // ── Phase 0 pre-check: is Tailscale session already valid? ────────────────
  // If session_credentials shows tailscale=valid for the mesh-admin, google-signin
  // is definitely not needed — skip ensure_google_session entirely.
  // This avoids all lock acquisition, browser launch, and Supabase round-trips.
  let _skipGoogleSignin = false;
  try {
    const { getSession: _tsPreCheck } = await import('file:///content/xio-browser/src/core/db.mjs');
    const _meshSess  = _tsPreCheck(MESH_ADMIN_SESSION);
    _skipGoogleSignin = !!(_meshSess?.services?.find?.(
      s => s.service === 'tailscale' && s.is_valid
    ));
    if (_skipGoogleSignin) {
      ctx.log(`[ts-signin] ✅ tailscale=valid in DB for ${MESH_ADMIN_SESSION} — skipping ensure_google_session entirely`);
    }
  } catch { /* db unavailable — run ensure_google_session normally */ }

  // ── Phase 0: Ensure mesh-admin Google session is fresh ────────────────────
  // Uses ctx.runInline() — runs google-signin in same process, nested sub-dir,
  // no job-queue deadlock. Auto-skips if session is still valid.
  //
  // DISTRIBUTED LOCK: Multiple Colab workers spawning simultaneously all call
  // tailscale-signin → google-signin for the same MESH_ADMIN_SESSION.
  // We use a Supabase-backed distributed lock to serialize these across nodes.
  // The lock is TTL-based (180s) so crashed nodes never permanently block others.
  await ctx.step('ensure_google_session', async () => {
    const _xiobr = '/content/xio-browser';
    const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
    let _distLock = false;

    // Pre-check: tailscale already valid → skip google-signin entirely
    if (_skipGoogleSignin) return;

    // Try to acquire distributed lock (Supabase-backed, cross-node)
    try {
      const { acquireSessionLock, releaseSessionLock } =
        await import(`file://${_xiobr}/src/core/db.mjs`);
      ctx.log(`[mesh-lock] Acquiring distributed lock for ${MESH_ADMIN_SESSION}…`);
      const _lockDeadline = Date.now() + 90_000; // wait up to 90s for lock
      while (Date.now() < _lockDeadline) {
        _distLock = await acquireSessionLock(MESH_ADMIN_SESSION, _nodeName, 180_000);
        if (_distLock) break;
        ctx.log(`[mesh-lock] Lock held by another node — retrying in 10s…`);
        await new Promise(r => setTimeout(r, 10_000));
      }
      if (!_distLock) {
        ctx.log(`[mesh-lock] Could not acquire lock after 90s — proceeding anyway`);
      } else {
        ctx.log(`[mesh-lock] Lock acquired for ${MESH_ADMIN_SESSION}`);
      }

      try {
        // ── Pre-check: DB says google=valid AND session JSON has Google cookies ──
        // DB is_valid alone is not sufficient — it persists in D1 across reboots even
        // when the session JSON (with actual cookies) is gone. Require BOTH conditions.
        const { getSession } = await import(`file://${_xiobr}/src/core/db.mjs`);
        const _preCheckSess  = getSession(MESH_ADMIN_SESSION);
        const _googleDbValid = !!(_preCheckSess?.services?.find?.(
          s => s.service === 'google' && s.is_valid
        ));
        let _googleFileOk = false;
        if (_googleDbValid) {
          try {
            const { existsSync: _ex, readFileSync: _rf } = await import('node:fs');
            const { sessionStatePath: _ssp } = await import(`file://${_xiobr}/src/core/session-manager.mjs`);
            const _fp = _ssp(MESH_ADMIN_SESSION);
            if (_ex(_fp)) {
              const _st = JSON.parse(_rf(_fp, 'utf8'));
              _googleFileOk = (_st.cookies || []).some(c => c.domain?.includes('google.com'));
            }
          } catch {}
          if (!_googleFileOk) {
            ctx.log(`[mesh-lock] ⚠️ DB says google=valid but session file missing — will run google-signin (Check 3: R2 profile)`);
          }
        }
        const _googleValid = _googleDbValid && _googleFileOk;
        if (_googleValid) {
          ctx.log(`[mesh-lock] ✅ google=valid (DB+file) for ${MESH_ADMIN_SESSION} — skipping google-signin inline`);

          // Release lock immediately — we're done
          if (_distLock) {
            await releaseSessionLock(MESH_ADMIN_SESSION, _nodeName);
            ctx.log(`[mesh-lock] Released lock for ${MESH_ADMIN_SESSION}`);
          }
          return; // step complete — session confirmed good, nothing to do
        }
        // Session not confirmed valid — run google-signin inline
        ctx.log(`Running google-signin inline for ${MESH_ADMIN_SESSION}…`);
        const result = await ctx.runInline('google-signin', MESH_ADMIN_SESSION);
        ctx.log(`google-signin inline completed: ok=${result.ok}`);
        if (!result.ok) {
          ctx.log(`google-signin failed — ${result.error ?? 'unknown'} — continuing anyway`);
        } else {
          ctx.log('Google session established');
        }
      } finally {
        if (_distLock) {
          await releaseSessionLock(MESH_ADMIN_SESSION, _nodeName);
          ctx.log(`[mesh-lock] Released lock for ${MESH_ADMIN_SESSION}`);
        }
      }
    } catch (lockErr) {
      ctx.log(`[mesh-lock] Lock unavailable (${lockErr.message?.slice(0, 60)}) — running google-signin unlocked`);
      const result = await ctx.runInline('google-signin', MESH_ADMIN_SESSION);
      ctx.log(`google-signin inline completed (unlocked): ok=${result.ok}`);
    }
  }, { throwOnFail: false });


  // ── Phase 1: Pull mesh-admin Chrome profile from Drive ────────────────────
  await ctx.step('prepare_mesh_admin_profile', async () => {
    const _xiobr = '/content/xio-browser';
    const { ensureSessionState, localProfilePath, restoreProfile } =
      await import(`file://${_xiobr}/src/core/session-manager.mjs`);
    ctx.log(`Ensuring mesh-admin profile (${MESH_ADMIN_SESSION}) is available locally…`);
    await ensureSessionState(MESH_ADMIN_SESSION);
    const profileDir = localProfilePath(MESH_ADMIN_SESSION);
    const { existsSync } = await import('node:fs');
    if (!existsSync(profileDir)) {
      restoreProfile(MESH_ADMIN_SESSION);
    }
    meshAdminProfileDir = profileDir;
    ctx.log(`Mesh-admin profile ready: ${meshAdminProfileDir}`);
  }, { throwOnFail: false });

  if (!meshAdminProfileDir) {
    meshAdminProfileDir = `/content/xio-mesh/chrome_profiles/PRFL-021_${MESH_ADMIN_SESSION}`;
    ctx.log(`Falling back to canonical PRFL path: ${meshAdminProfileDir}`);
  }


  // ── Step 2: Open browser, sign in to Tailscale ────────────────────────────
  await ctx.step('tailscale_signin', async () => {
    const { chromium: _pwr } = await import('patchright');

    // Load full session state (cookies + localStorage) from session JSON.
    // Using newContext({ storageState }) is more reliable than addCookies()
    // on a persistent context because it loads ALL session data, preventing
    // Google from showing the account as "Signed out" in the OAuth chooser.
    let storageState = undefined;
    try {
      const { loadStorageState } =
        await import('file:///content/xio-browser/src/core/session-manager.mjs');
      storageState = loadStorageState(MESH_ADMIN_SESSION);
      if (storageState?.cookies?.length) {
        ctx.log(`Loaded storageState: ${storageState.cookies.length} cookies, ${storageState.origins?.length ?? 0} origins`);
      } else {
        ctx.log('Warning: storageState empty or missing cookies');
      }
    } catch (se) {
      ctx.log(`storageState load skipped: ${se.message}`);
    }

    ctx.log('Launching browser with full session state…');
    const browser = await _pwr.launch({
      headless: true,   // ⚠️ Must be headless — visible Chrome on :99 crashes the Colab notebook.
      args: ['--no-sandbox', '--disable-setuid-sandbox'],
      ignoreDefaultArgs: ['--enable-automation'],
    });

    const adminCtx = await browser.newContext({ storageState });

    try {
      const tsPage = await adminCtx.newPage();
      const shot = createShot(_evDir, { logFn: ctx.log.bind(ctx) });
      shot.setPage(tsPage);


      // Navigate to Tailscale login (generic) — this step's job is only to ensure the
      // mesh-admin is signed into Tailscale admin console. Device auth (/a/...) is
      // handled by tailscale-auth.mjs which uses launchPersistentContext.
      ctx.log(`Navigating to ${TS_LOGIN_URL}`);
      await tsPage.goto(TS_LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await tsPage.waitForTimeout(3000);
      await shot('ts_signin_01_initial');
      const urlAfterNav = tsPage.url();
      ctx.log(`URL after navigation: ${urlAfterNav}`);

      if (urlAfterNav.includes(TS_CONSOLE_PATTERN)) {
        // Already signed in — persisted session worked
        ctx.log('Already signed into Tailscale — auto-redirected to console');
        await shot('ts_already_authed_console');
      } else {
        // Sign-in required
        ctx.log('Not signed in — looking for "Sign in with Google" button');
        await shot('ts_signin_02_login_page');

        const googleBtn = await tsPage.waitForSelector(
          'button:has-text("Sign in with Google"), a:has-text("Sign in with Google")',
          { timeout: 15_000, state: 'visible' }
        ).catch(() => null);

        if (!googleBtn) {
          await shot('ts_signin_fail_no_btn');
          throw new Error('Could not find "Sign in with Google" button on Tailscale login page');
        }

        await googleBtn.click();
        ctx.log('Clicked "Sign in with Google"');

        // Tailscale opens the Google account chooser in the SAME TAB (not a popup).
        // Wait for the page to navigate to accounts.google.com, then pick the account.
        ctx.log('Waiting for accounts.google.com navigation (same-tab flow)…');
        await tsPage.waitForURL('**/accounts.google.com/**', { timeout: 20_000 })
          .catch(() => ctx.log(`accounts.google.com wait timeout — URL: ${tsPage.url()}`));
        await tsPage.waitForTimeout(2000);
        await shot('ts_signin_03_google_chooser');
        ctx.log(`Account chooser URL: ${tsPage.url()}`);

        // Try popup as fallback (some environments do open a popup)
        const gPopup = tsPage.context().pages().find(p => p !== tsPage && p.url().includes('accounts.google.com')) ?? null;
        const chooserPage = gPopup ?? tsPage;


        // ── Pre-load mesh-admin credentials for 2FA ──────────────────────────
        let _tsSigninTotp = null, _tsSigninPwd = null;
        try {
          const { getAccount, getSession } =
            await import(`file:///content/xio-browser/src/core/db.mjs`);
          const _sess  = getSession(MESH_ADMIN_SESSION);
          const _email = _sess?.email ?? MESH_ADMIN_EMAIL;
          const _acct  = getAccount(_email);
          _tsSigninTotp = _acct?.totp_secret ?? null;
          _tsSigninPwd  = _acct?.password    ?? null;
          ctx.log(`[ts-signin] Credentials: totp=${!!_tsSigninTotp} pwd=${!!_tsSigninPwd}`);
        } catch (_ce) {
          ctx.log(`[ts-signin] Could not load credentials: ${_ce.message}`);
        }


        // ── Run shared Google OAuth loop ──────────────────────────────────────
        // Handles: account chooser (continue= bypass + data-identifier click),
        // challenge/dp (non-blocking notification + 30s polling + TOTP fallback),
        // challenge/totp (auto-fill), challenge/pwd (auto-fill), consent/allow screens.
        const _oaResult = await runGoogleOAuthLoop(chooserPage, {
          ctx,
          shot,
          accountEmail: MESH_ADMIN_EMAIL,
          totpSecret:   _tsSigninTotp,
          password:     _tsSigninPwd,
          maxRounds:    10,
          stepDir:      shot.dir ?? null,
          logPrefix:    '[ts-signin/oauth]',
          successUrl:   TS_CONSOLE_PATTERN,
        });
        ctx.log(`[ts-signin] OAuth loop: ok=${_oaResult.ok} url=${_oaResult.url?.slice(0, 80)} reason=${_oaResult.reason}`);
        await shot('ts_signin_04b_post_oauth');

        // If popup was used, wait for it to close and sync main page
        if (gPopup) {
          await gPopup.waitForEvent('close', { timeout: 30_000 }).catch(() => {});
          ctx.log('Google popup closed');
          await shot('ts_after_popup_closed');
        }


        // ── Device verification HITL detection (final safety net) ───────────────────────
        // This block is reached only if the OAuth loop exhausted all rounds without reaching
        // the Tailscale console AND the page still shows a device-verification prompt.
        // For challenge/dp this should NOT happen — the dp handler above (3×10s polls +
        // "More ways to verify" → TOTP) fully resolves it without HITL.
        // This fires only for unhandled challenges that slipped through (sk, ipp, az, etc.).
        const pageText = await tsPage.evaluate(() => document.body?.innerText ?? '').catch(() => '');
        const _curChallUrl = tsPage.url();
        const isDeviceVerify = /verify it.s you|check your|tap yes|a notification/i.test(pageText)
          && !_curChallUrl.includes('challenge/totp')
          && !_curChallUrl.includes('challenge/sk');
        if (isDeviceVerify && !tsPage.url().includes(TS_CONSOLE_PATTERN)) {
          ctx.log('🔐 Device verification screen still present after loop — pausing for HITL');
          await shot('ts_signin_device_verify');


          // Extract the 2-digit number shown on screen
          let verifyNum = '??';
          try {
            const numEl = await tsPage.$('strong').catch(() => null)
              ?? await tsPage.$('.rddEOc').catch(() => null)
              ?? await tsPage.$('.tRGaT').catch(() => null)
              ?? await tsPage.$('[class*="num"]').catch(() => null);
            if (numEl) verifyNum = ((await numEl.textContent()).trim()).replace(/\D/g, '') || '??';
          } catch {}
          ctx.log(`Device verification number: ${verifyNum}`);

          await ctx.hitl(
            `Google device verification required for Tailscale sign-in. Number shown: ${verifyNum}`,
            {
              instructions:
                `Google sent a security notification to your registered device (e.g. Realme Pad 2). ` +
                `Open the Google app → tap "Yes" on the security prompt → tap the number "${verifyNum}" shown on screen. ` +
                `Tell the agent when done — it will resume the workflow automatically.`,
            }
          );
          // Resumed — wait for redirect to console (user has verified)
          ctx.log('Resumed after device verification — waiting for Tailscale console redirect…');
          await tsPage.waitForURL(`**/${TS_CONSOLE_PATTERN}/**`, { timeout: 120_000 })
            .catch(() => ctx.log(`Post-HITL redirect timeout — URL: ${tsPage.url()}`));
        } else {
          // Normal flow — wait for redirect to console
          await tsPage.waitForURL(`**/${TS_CONSOLE_PATTERN}/**`, { timeout: 30_000 })
            .catch(() => ctx.log(`Redirect timeout — current URL: ${tsPage.url()}`));
        }

        await tsPage.waitForTimeout(2000);
        await shot('ts_signin_final');
        const _finalShotPath = shot.lastPath; // path of the just-written screenshot
        ctx.log(`Final URL: ${tsPage.url()}`);

        if (!tsPage.url().includes(TS_CONSOLE_PATTERN)) {
          // Check once more for device verify before throwing
          const stillVerify = /verify it.s you|check your/i.test(
            await tsPage.evaluate(() => document.body?.innerText ?? '').catch(() => '')
          );
          if (stillVerify) {
            throw new Error(`Device verification still pending after HITL resume — URL: ${tsPage.url()}`);
          }
          throw new Error(`Tailscale sign-in did not reach console — URL: ${tsPage.url()}`);
        }
      }

      // Save updated session state (now includes Tailscale console cookies too)
      try {
        const updatedState = await adminCtx.storageState();
        const _xiobr3 = '/content/xio-browser';
        const { saveStorageState } =
          await import(`file://${_xiobr3}/src/core/session-manager.mjs`);
        await saveStorageState(adminCtx, MESH_ADMIN_SESSION);
        const _cookieCount = updatedState.cookies?.length ?? 0;
        ctx.log(`Tailscale session persisted: ${_cookieCount} cookies saved`);
        ctx.setResult({ success: true, url: tsPage.url(), cookies_saved: _cookieCount });
      } catch (e) {
        ctx.log(`Could not persist session: ${e.message}`);
      }

      // Push session JSON + Chrome profile to all storage tiers via generalised helper.
      // Session JSON -> Supabase (awaited, 15 s) + Drive (background).
      // Chrome profile -> R2 (awaited, 60 s) + Drive (background).
      try {
        const _xiobr4 = '/content/xio-browser';
        const { pushToStorage } = await import(`file://${_xiobr4}/src/core/session-manager.mjs`);
        await pushToStorage(MESH_ADMIN_SESSION, {
          session: true,
          profile: true,
          awaitPrimary: true,
          log: msg => ctx.log(msg),
        });
      } catch (_pe) {
        ctx.log(`Push skipped: ${_pe.message}`);
      }


      // Record mesh-admin Tailscale service session
      try {
        const _xiobr = '/content/xio-browser';
        const { upsertServiceSession } = await import(`file://${_xiobr}/src/core/db.mjs`);
        upsertServiceSession({
          session_id:   MESH_ADMIN_SESSION,
          service:      'tailscale',
          account_hint: `${MESH_ADMIN_SESSION}@gmail.com`,
          is_valid:     true,
          node_name:    process.env.XIO_NODE_NAME ?? 'colab-master',
          metadata:     { verified_at: new Date().toISOString(), service_url: 'console.tailscale.com' },
        });
        ctx.log('[ts-signin] ✅ session_credentials updated: tailscale=valid for mesh-admin');
      } catch (_dbe) {
        ctx.log(`[ts-signin] session_credentials update skipped: ${_dbe.message}`);
      }

      // Copy final screenshot to job root for quick Drive review
      try {
        const { copyFileSync } = await import('node:fs');
        if (shot.lastPath) copyFileSync(shot.lastPath, path.join(_jobDir, 'result_final.jpg'));
      } catch {}

      // Set final success result (overrides any HITL-pending marker set during device verify)
      // Only set if not already set by the cookie block above to avoid overwriting cookies_saved
      if (!ctx._result?.success) {
        ctx.setResult({
          success:      true,
          url:          tsPage.url(),
          cookies_saved: null,
        });
      }

      // ── C6 fix: persist ephemeral context's storageState back to session JSON ──
      // Without this, cookies/localStorage generated during TS sign-in are lost
      // when the ephemeral context (newContext) closes.
      try {
        const freshState = await adminCtx.storageState();
        const { saveStorageStateFull } = await import('file:///content/xio-browser/src/core/session-manager.mjs');
        await saveStorageStateFull(MESH_ADMIN_SESSION, freshState);
        ctx.log(`[ts-signin] ✅ storageState saved back to session (${freshState.cookies?.length ?? 0} cookies)`);
      } catch (_ssErr) {
        ctx.log(`[ts-signin] ⚠️  storageState save skipped: ${_ssErr.message}`);
      }

      ctx.log('Tailscale sign-in workflow complete');
      await tsPage.close().catch(() => {});
    } finally {
      await adminCtx.close().catch(() => {});
      await browser.close().catch(() => {});
    }
  });
}
