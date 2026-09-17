/**
 * tailscale-auth.mjs — Tailscale device authorization workflow
 *
 * Uses the mesh-admin Chrome PROFILE DIR (persistent context, same as tailscale-signin)
 * to navigate to the Tailscale auth URL and click "Connect".
 *
 * Root-cause fix (vs prior implementation):
 *   - OLD: loaded a fresh browser context from JSON storageState → missing Tailscale
 *          session data stored in browser localStorage/IndexedDB → always ended at login page.
 *   - NEW: uses launchPersistentContext with the same profile dir as tailscale-signin,
 *          so all browser-level session state (cookies, localStorage, IndexedDB) is intact.
 *
 * Flow:
 *   1. Ensure mesh-admin session pulled fresh from Supabase
 *   2. Launch persistent context with mesh-admin profile dir
 *   3. Navigate to TS auth URL
 *   4. If "Sign in with Google" present: full sign-in → pick account → handle OAuth consent
 *      (including challenge/pwd password entry if needed)
 *   5. Click "Connect" button
 *   6. Wait for "Login successful" → console redirect
 *   7. Save session state + push to primary stores
 */

import { createShot } from '../src/core/wf-shot.mjs';
import { getMeshAdmin } from '../src/core/ecosystem.mjs';
import { runGoogleOAuthLoop } from '../src/core/google-oauth-loop.mjs';


// Mesh-admin resolved from ecosystem.mjs (config-driven via xio_config.json → networks)
const MESH_ADMIN_SESSION = getMeshAdmin('tailnet-primary');
const TS_AUTH_URL_FILE   = '/tmp/xio_ts_auth_url';
const _XIOBR             = '/content/xio-browser';

export async function run(ctx, params) {
  const _jobDir = ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : `/tmp/ts-auth-${Date.now()}`);
  const _evDir  = `${_jobDir}/steps`;
  const { mkdirSync, readFileSync } = await import('node:fs');
  mkdirSync(_evDir, { recursive: true });
  const shot = createShot(_evDir, { logFn: ctx.log.bind(ctx) });

  // ── Read TS auth URL ───────────────────────────────────────────────────────
  let tsUrl = params?.tsAuthUrl ?? params?.ts_auth_url ?? null;
  if (!tsUrl) {
    try {
      tsUrl = readFileSync(TS_AUTH_URL_FILE, 'utf8').trim();
      ctx.log(`TS auth URL from file: ${tsUrl}`);
    } catch (e) {
      throw new Error(`No ts_auth_url param and ${TS_AUTH_URL_FILE} not found: ${e.message}`);
    }
  }
  if (!tsUrl.includes('login.tailscale.com')) {
    throw new Error(`Invalid TS auth URL: ${tsUrl}`);
  }

  // ── Step 0: Ensure mesh-admin Google session is fresh (absorbed from tailscale-signin) ──
  // When called from self-spawn (params.ensureSession !== false), we run google-signin
  // inline for the mesh-admin account before touching the Tailscale auth URL.
  // Uses the same distributed lock as tailscale-signin to prevent concurrent
  // google-signin runs when multiple workers spawn simultaneously.
  // Skip by passing params.ensureSession = false (e.g. when called standalone after
  // tailscale-signin has already refreshed the session).
  if (params?.ensureSession !== false) {
    await ctx.step('ensure_google_session', async () => {
      const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
      let _distLock = false;

      // Fast-path: DB shows google=valid AND session JSON file has Google cookies (survives reboot)
      // NOTE: DB is_valid alone is not sufficient — it persists to D1 and survives reboots even
      // when the actual session JSON (with cookies) is gone. Require BOTH to skip google-signin.
      let _skipAll = false;
      try {
        const { getSession: _gg } = await import(`file://${_XIOBR}/src/core/db.mjs`);
        const _s = _gg(MESH_ADMIN_SESSION);
        const _svcs = _s?.services ?? [];
        const _gOk = _svcs.find(s => s.service === 'google'    && s.is_valid);
        const _tOk = _svcs.find(s => s.service === 'tailscale' && s.is_valid);

        // Check 2: session JSON file has Google cookies (proves session survived reboot)
        let _fileOk = false;
        if (_gOk) {
          try {
            const { existsSync: _ex, readFileSync: _rf } = await import('node:fs');
            const { sessionStatePath: _ssp } = await import(`file://${_XIOBR}/src/core/session-manager.mjs`);
            const _fp = _ssp(MESH_ADMIN_SESSION);
            if (_ex(_fp)) {
              const _st = JSON.parse(_rf(_fp, 'utf8'));
              _fileOk = (_st.cookies || []).some(c => c.domain?.includes('google.com'));
            }
          } catch {}
          if (!_fileOk) {
            ctx.log(`[ensure-session] ⚠️ DB says google=valid but session file missing/stale — will run google-signin (Check 3: R2 profile)`);
          }
        }

        if (_gOk && _fileOk && _tOk) {
          ctx.log(`[ensure-session] ✅ google=valid + tailscale=valid (DB+file) — skipping google-signin`);
          _skipAll = true;
        } else if (_gOk && _fileOk) {
          ctx.log(`[ensure-session] ✅ google=valid (DB+file) — skipping google-signin`);
          _skipAll = true;
        }
      } catch { /* db unavailable — run normally */ }


      if (!_skipAll) {
        // ── Distributed lock: only ONE runtime runs google-signin at a time ──────
        // Never run google-signin unlocked — concurrent runs on the same account
        // invalidate each other's sessions. Wait up to 5 min for the lock, then
        // re-check DB (another runtime may have already refreshed the session).
        let _distLock = false;
        try {
          const { acquireSessionLock, releaseSessionLock } =
            await import(`file://${_XIOBR}/src/core/db.mjs`);
          ctx.log(`[mesh-lock] Acquiring distributed lock for ${MESH_ADMIN_SESSION}…`);
          const _deadline = Date.now() + 300_000; // wait up to 5 min
          while (Date.now() < _deadline) {
            _distLock = await acquireSessionLock(MESH_ADMIN_SESSION, _nodeName, 300_000);
            if (_distLock) break;
            ctx.log(`[mesh-lock] Lock held by another node — retrying in 10s…`);
            await new Promise(r => setTimeout(r, 10_000));
          }

          if (!_distLock) {
            // Timeout: re-check DB — the lock holder may have already refreshed
            ctx.log(`[mesh-lock] ⚠️ Could not acquire lock after 5 min — re-checking DB…`);
            try {
              const { getSession: _gg2 } = await import(`file://${_XIOBR}/src/core/db.mjs`);
              const _s2 = _gg2(MESH_ADMIN_SESSION);
              const _gOk2 = _s2?.services?.find(s => s.service === 'google' && s.is_valid);
              if (_gOk2) {
                ctx.log(`[mesh-lock] google=valid in DB after wait — skipping google-signin safely`);
                return; // step succeeds, no google-signin needed
              }
            } catch {}
            ctx.log(`[mesh-lock] google still invalid after wait — skipping google-signin to avoid session conflict`);
            // Do NOT run unlocked — better to have a stale session than to invalidate another runtime's live session
            return;
          }

          ctx.log(`[mesh-lock] Lock acquired`);
          try {
            const result = await ctx.runInline('google-signin', MESH_ADMIN_SESSION);
            ctx.log(`google-signin inline: ok=${result.ok}`);
            if (!result.ok) ctx.log(`google-signin failed: ${result.error ?? 'unknown'} — continuing`);
          } finally {
            await releaseSessionLock(MESH_ADMIN_SESSION, _nodeName);
            ctx.log(`[mesh-lock] Released lock`);
          }
        } catch (lockErr) {
          // Lock infrastructure error — re-check DB before running unlocked
          ctx.log(`[mesh-lock] Lock error (${lockErr.message?.slice(0, 60)}) — checking DB before proceeding`);
          try {
            const { getSession: _gg3 } = await import(`file://${_XIOBR}/src/core/db.mjs`);
            const _s3 = _gg3(MESH_ADMIN_SESSION);
            const _gOk3 = _s3?.services?.find(s => s.service === 'google' && s.is_valid);
            if (_gOk3) {
              ctx.log(`[mesh-lock] google=valid in DB — skipping google-signin`);
              return;
            }
          } catch {}
          // Last resort: run google-signin, but only if truly no session
          ctx.log(`[mesh-lock] ⚠️ Running google-signin without lock (lock unavailable, session invalid)`);
          const result = await ctx.runInline('google-signin', MESH_ADMIN_SESSION);
          ctx.log(`google-signin inline (unlocked, last resort): ok=${result.ok}`);
        }
      }
    }, { throwOnFail: false });
  }

  // ── Step 1: Load mesh-admin session + resolve profile dir ─────────────────
  let profileDir;
  await ctx.step('load_mesh_admin_session', async () => {
    const { ensureSessionState, localProfilePath } =
      await import(`file://${_XIOBR}/src/core/session-manager.mjs`);

    // Always pull fresh session from primary stores (sessions + profile tarball)
    await ensureSessionState(MESH_ADMIN_SESSION);

    // Use the PERSISTENT Chrome profile dir — this is the key difference from
    // tailscale-signin (which uses newContext+storageState). Tailscale stores its
    // session in localStorage/IndexedDB; only launchPersistentContext accesses that.
    profileDir = localProfilePath(MESH_ADMIN_SESSION);
    ctx.log(`Mesh-admin profile dir: ${profileDir}`);

    shot.setPage(ctx.page);
    await shot('load_mesh_admin_session');
  }, { throwOnFail: false });

  // ── Authorize device via TS auth URL ──────────────────────────────────────
  await ctx.step('connect_device', async () => {
    const { chromium: _pwr } = await import('patchright');

    // PERSISTENT CONTEXT — preserves all browser-level state (localStorage, IndexedDB, etc.)
    // that tailscale-signin established. This is the fix for the stale-session bug.
    const _profilePath = profileDir ?? `/tmp/ts-auth-profile-${Date.now()}`;
    ctx.log(`Launching persistent context at: ${_profilePath}`);
    const adminCtx = await _pwr.launchPersistentContext(_profilePath, {
      headless: true,   // ⚠️ Must be headless — running visible Chrome on the same :99
                        // display as the main browser crashes the Colab notebook cell.
      args: ['--no-sandbox', '--disable-setuid-sandbox'],
      ignoreDefaultArgs: ['--enable-automation'],
    });

    try {
      const tsPage = await adminCtx.newPage();
      shot.setPage(tsPage);

      // ── 1. Verify Tailscale session ─────────────────────────────────────────
      ctx.log('Checking Tailscale session at login.tailscale.com…');
      await tsPage.goto('https://login.tailscale.com/login',
        { waitUntil: 'domcontentloaded', timeout: 20_000 });
      await tsPage.waitForTimeout(2000);
      await shot('ts_session_check');
      const checkUrl = tsPage.url();
      ctx.log(`Session check URL: ${checkUrl}`);
      if (checkUrl.includes('console.tailscale.com')) {
        ctx.log('✅ Tailscale session active — profile has live session');
      } else {
        ctx.log('⚠️ Tailscale session not active — will sign in at auth URL');
      }

      // ── 2. Navigate to TS auth URL ──────────────────────────────────────────
      ctx.log(`Navigating to TS auth URL: ${tsUrl}`);
      await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await tsPage.waitForTimeout(3000);
      await shot('ts_auth_url_loaded');
      ctx.log(`Auth URL page: ${tsPage.url().slice(0, 120)}`);

      // ── 3. Sign in with Google if required ─────────────────────────────────
      const ADMIN_EMAIL = `${MESH_ADMIN_SESSION}@gmail.com`;

      // Pre-load account credentials for 2FA (TOTP + password)
      let _adminTotp = null, _adminPwd = null;
      try {
        const { getAccount } = await import(`file://${_XIOBR}/src/core/db.mjs`);
        const _acct = getAccount(ADMIN_EMAIL);
        _adminTotp = _acct?.totp_secret ?? null;
        _adminPwd  = _acct?.password    ?? null;
        ctx.log(`Credentials loaded: totp=${!!_adminTotp} pwd=${!!_adminPwd}`);
      } catch (_ce) {
        ctx.log(`⚠️ Could not load credentials: ${_ce.message}`);
      }

      const gBtn = await tsPage.waitForSelector(
        'button:has-text("Sign in with Google"), a:has-text("Sign in with Google")',
        { timeout: 8_000, state: 'visible' }
      ).catch(() => null);

      if (gBtn) {
        ctx.log('Sign in with Google button found — clicking');
        await gBtn.click();

        // Wait for Google (account chooser / consent page)
        await tsPage.waitForURL('**/accounts.google.com/**', { timeout: 20_000 })
          .catch(() => ctx.log(`Post-click URL: ${tsPage.url().slice(0, 80)}`));
        await tsPage.waitForTimeout(2000);
        await shot('google_chooser');
        ctx.log(`Google redirect: ${tsPage.url().slice(0, 100)}`);

        // ── Run shared Google OAuth loop (handles chooser, dp, totp, pwd, consent) ──
        const _oaResult = await runGoogleOAuthLoop(tsPage, {
          ctx,
          shot,
          accountEmail: ADMIN_EMAIL,
          totpSecret:   _adminTotp,
          password:     _adminPwd,
          maxRounds:    10,
          stepDir:      _evDir,
          logPrefix:    '[ts-auth/oauth]',
        });
        ctx.log(`OAuth loop result: ok=${_oaResult.ok} url=${_oaResult.url?.slice(0, 80)} reason=${_oaResult.reason}`);
        await shot('after_google_signin');

        // If still on Google, wait for natural redirect
        if (tsPage.url().includes('accounts.google.com')) {
          ctx.log('[oauth] Still on Google — waiting up to 8s for redirect…');
          await tsPage.waitForURL(
            url => !url.toString().includes('accounts.google.com'),
            { timeout: 8000 }
          ).catch(() => {});
          ctx.log(`Redirect settled: ${tsPage.url().slice(0, 100)}`);
        }

        // If landed on tailscale login (not the auth URL), re-navigate
        const _postSigninUrl = tsPage.url();
        if (_postSigninUrl.includes('login.tailscale.com') &&
            !_postSigninUrl.includes('/a/')) {
          ctx.log('Re-navigating to TS auth URL after sign-in…');
          await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
          await tsPage.waitForTimeout(3000);
        }

      } else {

        ctx.log('No "Sign in with Google" button — checking if page redirected directly to Google…');

        // ── Direct-to-Google redirect (no Sign-in button) ─────────────────────
        // Tailscale sessions often redirect directly to accounts.google.com/accountchooser
        // when the Tailscale session cookie is partially valid.
        // FIX: When URL has authuser=0, session is already authenticated.
        //      Extract the `continue` URL param and navigate there directly —
        //      this bypasses the account chooser UI entirely (which uses
        //      data-identifier NOT data-email, making locator clicks fail).
        if (tsPage.url().includes('accounts.google.com')) {
          ctx.log(`Page is on Google (${tsPage.url().slice(0, 80)}) — running shared OAuth loop…`);
          await shot('direct_google_redirect');

          // ── Run shared Google OAuth loop ─────────────────────────────────────
          // Handles: account chooser bypass (continue= URL), challenge/dp with
          // non-blocking notification + 30s polling + TOTP fallback, challenge/totp,
          // challenge/pwd, and consent/allow screens.
          const _dr = await runGoogleOAuthLoop(tsPage, {
            ctx,
            shot,
            accountEmail: ADMIN_EMAIL,
            totpSecret:   _adminTotp,
            password:     _adminPwd,
            maxRounds:    10,
            stepDir:      _evDir,
            logPrefix:    '[ts-auth/direct]',
          });
          ctx.log(`Direct-redirect OAuth result: ok=${_dr.ok} url=${_dr.url?.slice(0, 80)} reason=${_dr.reason}`);

          // Re-navigate to TS auth URL if we ended up on Tailscale login (not /a/)
          if (tsPage.url().includes('login.tailscale.com') && !tsPage.url().includes('/a/')) {
            ctx.log('Re-navigating to TS auth URL after Google flow…');
            await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
            await tsPage.waitForTimeout(3000);
          }

        } else {
          ctx.log('Session appears active — proceeding to Connect button');
        }
      }

    // ── CONCURRENT SAFETY: acquire mesh-admin lock if Google re-auth needed ──
    // If the connect_device step needs to run Google OAuth (expired cookies), we must
    // acquire the distributed mesh-admin lock to prevent two concurrent runtimes from
    // both re-authenticating the same account — which invalidates each other's sessions.
    // We check first; if the session is already valid we skip the lock and just click Connect.
    let _deviceLock = false;
    const _needsReauth = await (async () => {
      try {
        const _ts = await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 30_000 });
        await tsPage.waitForTimeout(3000);
        const _u = tsPage.url();
        // If already at the auth URL (/a/ path) or console, no re-auth needed
        if (_u.includes('/a/') || _u.includes('console.tailscale.com')) return false;
        // If on Google sign-in, re-auth needed
        if (_u.includes('accounts.google.com') || _u.includes('accounts.google.com')) return true;
        // If "Sign in with Google" button present, re-auth needed
        const btn = await tsPage.waitForSelector(
          'button:has-text("Sign in with Google"), a:has-text("Sign in with Google")',
          { timeout: 5000, state: 'visible' }
        ).catch(() => null);
        return !!btn;
      } catch { return false; }
    })();

    if (_needsReauth) {
      ctx.log('[ts-auth] Google re-auth required for connect_device — acquiring mesh-admin lock…');
      try {
        const { acquireSessionLock, releaseSessionLock } =
          await import(`file://${_XIOBR}/src/core/db.mjs`);
        const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
        const _deadline = Date.now() + 300_000; // wait up to 5 min
        while (Date.now() < _deadline) {
          _deviceLock = await acquireSessionLock(`${MESH_ADMIN_SESSION}:ts-device`, _nodeName, 300_000);
          if (_deviceLock) { ctx.log('[ts-auth] Device-auth lock acquired'); break; }
          // While waiting, check if another runtime already refreshed — re-check session
          const _url2 = tsPage.url();
          if (_url2.includes('/a/') || _url2.includes('console.tailscale.com')) {
            ctx.log('[ts-auth] Session refreshed by another runtime while waiting — proceeding');
            break;
          }
          ctx.log('[ts-auth] Lock held — waiting 15s…');
          await new Promise(r => setTimeout(r, 15_000));
        }
        if (!_deviceLock) ctx.log('[ts-auth] Could not acquire device lock — proceeding (may race)');
      } catch (_le) {
        ctx.log(`[ts-auth] Lock error: ${_le.message?.slice(0, 60)} — proceeding`);
      }
    }

    try {
      // ── Run Google sign-in if needed (already navigated to tsUrl above) ─────
      const _curUrl = tsPage.url();
      if (_curUrl.includes('accounts.google.com')) {
        ctx.log(`Page is on Google (${_curUrl.slice(0, 80)}) — running shared OAuth loop…`);
        await shot('direct_google_redirect');
        const _dr = await runGoogleOAuthLoop(tsPage, {
          ctx, shot, accountEmail: ADMIN_EMAIL, totpSecret: _adminTotp, password: _adminPwd,
          maxRounds: 10, stepDir: _evDir, logPrefix: '[ts-auth/direct]',
        });
        ctx.log(`Direct-redirect OAuth result: ok=${_dr.ok} url=${_dr.url?.slice(0, 80)} reason=${_dr.reason}`);
        if (tsPage.url().includes('login.tailscale.com') && !tsPage.url().includes('/a/')) {
          await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
          await tsPage.waitForTimeout(3000);
        }
      } else {
        const gBtn = await tsPage.waitForSelector(
          'button:has-text("Sign in with Google"), a:has-text("Sign in with Google")',
          { timeout: 5000, state: 'visible' }
        ).catch(() => null);
        if (gBtn) {
          ctx.log('Sign in with Google button found — clicking');
          await gBtn.click();
          await tsPage.waitForURL('**/accounts.google.com/**', { timeout: 20_000 })
            .catch(() => ctx.log(`Post-click URL: ${tsPage.url().slice(0, 80)}`));
          await tsPage.waitForTimeout(2000);
          await shot('google_chooser');
          const _oaResult = await runGoogleOAuthLoop(tsPage, {
            ctx, shot, accountEmail: ADMIN_EMAIL, totpSecret: _adminTotp, password: _adminPwd,
            maxRounds: 10, stepDir: _evDir, logPrefix: '[ts-auth/oauth]',
          });
          ctx.log(`OAuth loop result: ok=${_oaResult.ok} reason=${_oaResult.reason}`);
          await shot('after_google_signin');
          if (tsPage.url().includes('login.tailscale.com') && !tsPage.url().includes('/a/')) {
            await tsPage.goto(tsUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
            await tsPage.waitForTimeout(3000);
          }
        } else {
          ctx.log('Session appears active — proceeding to Connect button');
        }
      }
    } finally {
      // Release device-auth lock as soon as Google phase is done
      if (_deviceLock) {
        try {
          const { releaseSessionLock } = await import(`file://${_XIOBR}/src/core/db.mjs`);
          const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
          await releaseSessionLock(`${MESH_ADMIN_SESSION}:ts-device`, _nodeName);
          ctx.log('[ts-auth] Device-auth lock released');
          _deviceLock = false;
        } catch (_) {}
      }
    }

      // ── 4. Click "Connect" button ──────────────────────────────────────────

      ctx.log('Waiting for Connect/Authorize button…');
      const connectBtn = await tsPage.waitForSelector(
        'button:has-text("Connect"), button:has-text("Authorize"), ' +
        '[data-testid="connect-button"], button:has-text("Allow")',
        { timeout: 30_000, state: 'visible' }
      ).catch(() => null);

      if (connectBtn) {
        const btnText = await connectBtn.evaluate(el => el.textContent?.trim()).catch(() => 'Connect');
        ctx.log(`✅ "${btnText}" button visible — clicking`);
        await shot('connect_btn_visible');
        await connectBtn.click();
        await tsPage.waitForTimeout(5000);
        await shot('after_connect');
        ctx.log(`Post-connect URL: ${tsPage.url()}`);

        // Handle Google popup if Connect triggers one
        const gPopup = adminCtx.pages().find(p => p !== tsPage && p.url().includes('accounts.google.com')) ?? null;
        if (gPopup) {
          ctx.log(`Google popup appeared: ${gPopup.url()}`);
          const acctEl = await gPopup.waitForSelector(
            `[data-email="${MESH_ADMIN_SESSION}@gmail.com"], [data-email="${MESH_ADMIN_SESSION}"], [data-email]`,
            { timeout: 8_000, state: 'visible' }
          ).catch(() => null);
          if (acctEl) {
            await acctEl.click();
          } else {
            ctx.log('⚠️  Account element not found in popup — closing it');
            await gPopup.close().catch(() => {});
          }
          await gPopup.waitForEvent('close', { timeout: 10_000 }).catch(() => {
            gPopup.close().catch(() => {});
          });
          ctx.log('Google popup closed');
          await tsPage.waitForTimeout(2000);
          await shot('after_popup');
        }

        // Wait for console redirect ("Login successful" → console)
        await tsPage.waitForURL('**/console.tailscale.com/**', { timeout: 20_000 })
          .catch(() => ctx.log(`Post-connect URL (no console redirect): ${tsPage.url()}`));
        await tsPage.waitForTimeout(2000);
        await shot('ts_final');
        ctx.log(`✅ Final URL: ${tsPage.url()}`);

        if (tsPage.url().includes('console.tailscale.com')) {
          ctx.log('✅ Device authorized — at Tailscale console!');
        } else {
          ctx.log(`⚠️ Unexpected final URL — may still be authorized: ${tsPage.url()}`);
        }

      } else {
        // Check if already connected (redirected before we could click)
        const finalUrl = tsPage.url();
        if (finalUrl.includes('console.tailscale.com')) {
          ctx.log('✅ Already at console — device may already be authorized');
          await shot('already_at_console');
        } else {
          await shot('ts_no_connect_btn');
          throw new Error(`No Connect/Authorize button found. URL: ${finalUrl}`);
        }
      }

      // ── 5. Save + push updated session state (with distributed lock) ───────
      // Lock prevents concurrent runtimes from overwriting each other's session saves.
      let _saveLock = false;
      try {
        const { acquireSessionLock } = await import(`file://${_XIOBR}/src/core/db.mjs`);
        const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
        const _saveDeadline = Date.now() + 60_000; // wait up to 60s for save lock
        while (Date.now() < _saveDeadline) {
          _saveLock = await acquireSessionLock(`${MESH_ADMIN_SESSION}:session-save`, _nodeName, 120_000);
          if (_saveLock) { ctx.log('[ts-auth] Session-save lock acquired'); break; }
          ctx.log('[ts-auth] Session-save lock held — waiting 5s…');
          await new Promise(r => setTimeout(r, 5_000));
        }
      } catch (_) {}

      try {
        const { saveStorageStateFull } =
          await import(`file://${_XIOBR}/src/core/session-manager.mjs`);
        await saveStorageStateFull(MESH_ADMIN_SESSION, tsPage);
        ctx.log('Full session state saved after device auth');
      } catch (e) {
        ctx.log(`saveStorageStateFull failed: ${e.message} — falling back to basic save`);
        try {
          const { saveStorageState } =
            await import(`file://${_XIOBR}/src/core/session-manager.mjs`);
          const snap = await adminCtx.storageState();
          await saveStorageState(MESH_ADMIN_SESSION, snap);
          ctx.log('Fallback basic session saved');
        } catch (e2) {
          ctx.log(`Basic save also failed: ${e2.message}`);
        }
      }

      try {
        const { pushToStorage } =
          await import(`file://${_XIOBR}/src/core/session-manager.mjs`);
        await pushToStorage(MESH_ADMIN_SESSION, {
          session: true, profile: true, awaitPrimary: true, log: ctx.log.bind(ctx),
        });
      } catch (_pe) {
        ctx.log(`Push skipped: ${_pe.message}`);
      } finally {
        if (_saveLock) {
          try {
            const { releaseSessionLock } = await import(`file://${_XIOBR}/src/core/db.mjs`);
            const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
            await releaseSessionLock(`${MESH_ADMIN_SESSION}:session-save`, _nodeName);
            ctx.log('[ts-auth] Session-save lock released');
          } catch (_) {}
        }
      }

      // ── 6. Record Tailscale service session in DB ─────────────────────────
      try {
        const { upsertServiceSession } = await import(`file://${_XIOBR}/src/core/db.mjs`);
        const workerSlug = (ctx.sessionId ?? MESH_ADMIN_SESSION)
          .split('@')[0].split('+')[0].replace(/[._]/g, '_');
        upsertServiceSession({
          session_id:   workerSlug,
          service:      'tailscale',
          account_hint: 'tailscale-device-auth',
          is_valid:     true,
          node_name:    process.env.XIO_NODE_NAME ?? 'colab-master',
          metadata:     { authorized_at: new Date().toISOString(), auth_method: 'google-oauth' },
        });
        ctx.log('[ts-auth] ✅ session_credentials updated: tailscale=valid for ' + workerSlug);
      } catch (_dbe) {
        ctx.log(`[ts-auth] session_credentials update skipped: ${_dbe.message}`);
      }

      await tsPage.close().catch(() => {});
    } finally {
      await adminCtx.close().catch(() => {});
    }
  });
}

