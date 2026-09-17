/**
 * google-signin.mjs
 * ─────────────────────────────────────────────────────────────────────────────
 * Google login workflow — Hybrid Stealth Stack (uc engine)
 *
 * Architecture:
 *   Phase 0: Check if already logged in (skip if force=true)
 *   Phase 1: Run uc (undetected-chromedriver) stealth sidecar with CDP 2FA
 *   Phase 2: Load session cookies from sidecar into patchright context
 *   Phase 3: Verify login via myaccount.google.com
 *
 * Profile Naming:
 *   Chrome profiles are stored under a DETERMINISTIC name derived from the
 *   email address — not from session_id (which is arbitrary):
 *     profileId('shahid.workload@gmail.com') → 'shahid_workload'
 *     profileId('user+alias@gmail.com')      → 'user'
 *   This makes profiles self-describing and stable across reboots.
 *
 * Params:
 *   email        - Google account email address (required)
 *   password     - Account password (required)
 *   totp_secret  - Base32 TOTP secret for 2FA (required)
 *   force        - Skip Phase 0 session check (default: false)
 */

export const meta = {
  name:        'google-signin',
  description: 'Full Google sign-in using undetected-chromedriver (uc) + CDP 2FA. Persists session cookies to Supabase (primary) + Drive (cold backup). Chrome profile stored on R2 (primary) + Drive (cold backup). Deterministic profile name derived from email.',
  requires:    [],
  params: {
    email:       'Google account email (required)',
    password:    'Account password (required)',
    totp_secret: 'Base32 TOTP secret for 2FA (required)',
    force:       '(optional) true = skip Phase 0 session check, default false',
  },
};

import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath }            from 'node:url';
import { resolve as _resolve, dirname as _dirname } from 'node:path';
import { createLogger }             from '../src/utils/logger.mjs';
import { createShot } from '../src/core/wf-shot.mjs';

const log = createLogger('gdt-login');

const WORKFLOW_ID = 'google-signin';
const DOMAIN      = 'accounts.google.com';

// ── Locate xio-browser root ───────────────────────────────────────────────
// Workflows can be loaded from Drive (xio-mesh/workflows/) or from the git
// repo (xio-browser/workflows/). Both cases need to reach src/core/.
// We walk up from this file and look for the known src/core/ sentinel.
const _thisDir = _dirname(fileURLToPath(import.meta.url));

function _findXiobrRoot(fromDir) {
  // Check: fromDir/../src/core/stealth-runner.mjs  (bundled: workflows/ is child of root)
  const candidate1 = _resolve(fromDir, '..', 'src', 'core', 'stealth-runner.mjs');
  if (existsSync(candidate1)) return _resolve(fromDir, '..');
  // Check: fromDir/../../src/core/stealth-runner.mjs  (Drive: xio-mesh/workflows/)
  const candidate2 = _resolve(fromDir, '..', '..', 'src', 'core', 'stealth-runner.mjs');
  if (existsSync(candidate2)) return _resolve(fromDir, '../..');
  // Fallback: absolute Colab path (always correct on Colab)
  return '/content/xio-browser';
}
const _xiobr = _findXiobrRoot(_thisDir);

// ── Lazy-load stealth-runner and engine-selector ──────────────────────────
let _stealthRunner, _getRankedEngines, _recordEngineResult;
try {
  _stealthRunner = await import(`file://${_xiobr}/src/core/stealth-runner.mjs`);
} catch (e) {
  log.warn(`[gdt-login] stealth-runner not available: ${e.message}`);
  _stealthRunner = {
    runStealthSidecar: async () => ({ success: false, engine: null }),
  };
}

try {
  const esr = await import(`file://${_xiobr}/src/core/engine-selector.mjs`);
  _getRankedEngines   = esr.getRankedEngines;
  _recordEngineResult = esr.recordEngineResult;
} catch (_) {
  // ESR not available — default to uc only (camoufox/nodriver removed)
  _getRankedEngines   = () => ['uc'];
  _recordEngineResult = () => {};
}

const { runStealthSidecar }    = _stealthRunner;
const getRankedEngines         = _getRankedEngines;
const recordEngineResult       = _recordEngineResult;

// ── Per-session Drive lock for google-signin ─────────────────────────────
// Prevents concurrent runtimes from running google-signin for the same account.
async function acquireSigninLock(sessionId, ctx, timeoutMs = 90_000) {
  // Use a local file lock instead of Drive — boot.py import takes ~60s and
  // causes ETIMEDOUT. All google-signin jobs run on the same Colab node so
  // a /tmp fcntl lock is sufficient for mutual exclusion.
  // Write lock script to temp .py file to avoid all shell-quoting issues.
  const { execSync: _es } = await import('node:child_process');
  const { writeFileSync, unlinkSync, existsSync } = await import('node:fs');
  const slug = sessionId.split('@')[0].replace(/[.+]/g, '_').slice(0, 40);
  const lockFile = `/tmp/xio_signin_${slug}.lock`;
  const lockScript = `/tmp/xio_lock_acquire_${slug}.py`;
  // Clear stale lock file left by a crashed Node process (older than 10 min)
  try {
    const { statSync, unlinkSync: _unlink } = await import('node:fs');
    const st = statSync(lockFile);
    if (Date.now() - st.mtimeMs > 600_000) {
      _unlink(lockFile);
      ctx.log(`[signin-lock] Cleared stale lock file (>10 min old)`);
    }
  } catch { /* file doesn't exist — expected */ }
  writeFileSync(lockScript, [
    'import fcntl, os, sys',
    `f = open(${JSON.stringify(lockFile)}, 'w')`,
    'try:',
    '    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)',
    '    f.write(str(os.getpid()))',
    '    f.flush()',
    "    print('ok')",
    'except BlockingIOError:',
    "    print('locked')",
  ].join('\n'));
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = _es(`python3 ${lockScript}`, { encoding: 'utf8', timeout: 5000 }).trim();
      if (res === 'ok') {
        ctx.log(`[signin-lock] Acquired lock for ${slug}`);
        try { unlinkSync(lockScript); } catch {}
        return lockFile;
      }
      ctx.log(`[signin-lock] Waiting for lock on ${slug}…`);
    } catch (_e) {
      ctx.log(`[signin-lock] Lock error: ${_e.message?.slice(0, 60)} — proceeding unlocked`);
      try { unlinkSync(lockScript); } catch {}
      return null;
    }
    await new Promise(r => setTimeout(r, 5000));
  }
  try { unlinkSync(lockScript); } catch {}
  ctx.log(`[signin-lock] Lock timeout for ${slug} — proceeding anyway`);
  return null;
}

async function releaseSigninLock(lockFile, ctx) {
  if (!lockFile) return;
  try {
    const { execSync: _es } = await import('node:child_process');
    _es(`rm -f ${JSON.stringify(lockFile)}`, { encoding: 'utf8', timeout: 3000 });
    ctx.log(`[signin-lock] Released lock ${lockFile}`);
  } catch (_e) {
    ctx.log(`[signin-lock] Lock release error: ${_e.message?.slice(0, 60)}`);
  }
}


// ── Helpers ───────────────────────────────────────────────────────────────────

const randInt    = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
const humanSleep = (a = 300, b = 900) => new Promise(r => setTimeout(r, randInt(a, b)));

/**
 * Deterministic Chrome profile ID derived from a Google email address.
 *   'shahid.workload@gmail.com'         → 'shahid_workload'
 *   'samnurnihartalukdar@gmail.com'     → 'samnurnihartalukdar'
 *   'user+alias@gmail.com'              → 'user'
 */
export function profileId(email) {
  return email.split('@')[0].split('+')[0].replace(/\./g, '_');
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN EXPORT
// ─────────────────────────────────────────────────────────────────────────────

export async function run(ctx, params = {}) {
  const page  = ctx.page;
  // Use ctx.log when available (structured, captured per-step); fall back to module logger
  const _log  = ctx.log?.bind(ctx) ?? log.info.bind(log);
  const { email: rawEmail, password: rawPassword, totp_secret: rawTotp, force = false } = params;

  // ── Auto-fetch credentials from accounts table when not supplied as params ──
  let email = rawEmail, password = rawPassword, totp_secret = rawTotp;

  // If email not in params, resolve from session record (account_email → slug@gmail.com)
  if (!email) {
    const { getSession } = await import(`file://${_xiobr}/src/core/db.mjs`);
    const session = getSession(ctx.sessionId);
    const _rawId = session?.account_email || ctx.sessionId;
    email = (_rawId && _rawId.includes('@')) ? _rawId : (_rawId ? `${_rawId}@gmail.com` : null);
  }

  if (!email) throw new Error('google-signin: email is required (not in params and could not resolve from session)');

  // Auto-fetch password / totp_secret from accounts table (loaded from Supabase into memory)
  if (!password || !totp_secret) {
    const { getAccount } = await import(`file://${_xiobr}/src/core/db.mjs`);
    const acct = getAccount(email);
    if (!password)    password    = acct?.password    ?? null;
    if (!totp_secret) totp_secret = acct?.totp_secret ?? null;
  }

  if (!email || !password) throw new Error('google-signin: email and password required (not found in accounts table either)');

  // sessionId comes from ctx (set by job-manager)
  const sessionId = ctx.sessionId ?? 'unknown';
  const jobId     = ctx.jobId ?? 'nojob';

  // Use canonical session path (PRFL-NNN_username.json) so that phase1 (stealth sidecar)
  // and phase2 (load cookies into patchright) agree on the same file.
  // Falls back to legacy slug path only if session-manager is unavailable.
  let sessionPath;
  try {
    const { sessionStatePath } = await import(`file://${_xiobr}/src/core/session-manager.mjs`);
    sessionPath = sessionStatePath(email);
  } catch {
    sessionPath = `/content/xio-mesh/sessions/${sessionId}.json`;
  }
  // Chrome profile dir
  const { localProfilePath: _localProfilePath } = await import(
    'file:///content/xio-browser/src/core/session-manager.mjs'
  );
  const profileDir = _localProfilePath(sessionId);
  // Use per-job dir so sidecar screenshots land in the orchestrator's file listing.
  // ctx.jobDir = full absolute path (correct for nested sub-workflow dirs)
  // ctx.dirName = leaf name only — use as fallback if jobDir not set
  const jobDir         = ctx.jobDir ?? `/content/xio-mesh/jobs/${ctx.dirName ?? jobId}`;
  const screenshotDir  = `${jobDir}/steps`;
  const shot = createShot(screenshotDir, { logFn: _log });
  shot.setPage(page);

  // ── Phase 0: Already logged in? ────────────────────────────────────────────
  // Skip when force=true (caller wants a fresh sidecar run regardless)
  let alreadyLoggedIn = false;
  if (force) {
    _log('[gdt-login] Phase 0: Skipped (force=true)');
  } else {
    await ctx.step('phase0_session_check', async () => {
      _log('[gdt-login] Phase 0: Session check...');

      // ── Check 1: DB is_valid flag (fast, no browser needed)
      // NOTE: After a runtime reboot the SQLite DB is re-loaded from D1 but
      // the is_valid flag is NOT persisted between runtimes. So this alone is
      // unreliable — we always also check the session JSON file on disk.
      let dbSaysValid = false;
      try {
        const { getSession } = await import(`file://${_xiobr}/src/core/db.mjs`);
        const _sess = getSession(sessionId);
        dbSaysValid = !!(_sess?.services?.find?.(s => s.service === 'google' && s.is_valid));
      } catch { /* db unavailable */ }

      // ── Check 2: Session JSON file exists with Google cookies (survives reboot)
      // Even after reboot, google-signin from a previous run may have saved cookies.
      let fileHasGoogleCookies = false;
      try {
        const { readFileSync, existsSync } = await import('node:fs');
        const { sessionStatePath } = await import(`file://${_xiobr}/src/core/session-manager.mjs`);
        const filePath = sessionStatePath(sessionId);
        if (existsSync(filePath)) {
          const state = JSON.parse(readFileSync(filePath, 'utf8'));
          fileHasGoogleCookies = (state.cookies || []).some(c =>
            c.domain && (c.domain.includes('google.com') || c.domain.includes('accounts.google'))
          );
          _log(`[gdt-login] Phase 0: session file found — google cookies=${fileHasGoogleCookies} (${(state.cookies||[]).length} total)`);
        } else {
          _log('[gdt-login] Phase 0: no session file on disk');
        }
      } catch (e) { _log(`[gdt-login] Phase 0: session file check error: ${e.message}`); }

      const looksLikeValid = dbSaysValid || fileHasGoogleCookies;
      _log(`[gdt-login] Phase 0: db_valid=${dbSaysValid} file_cookies=${fileHasGoogleCookies} → looksValid=${looksLikeValid}`);

      if (looksLikeValid) {
        // Session may be valid — do a live patchright check
        await page.goto('https://myaccount.google.com/', {
          waitUntil: 'domcontentloaded', timeout: 15000,
        }).catch(() => {});
        await humanSleep(800, 1500);

        const { verifyGoogleSession } = await import('file://' + _xiobr + '/src/core/session-verifier.mjs');
        const isValid = await verifyGoogleSession(page, email);
        await shot('phase0_session_check', { waitMs: 400 });

        if (isValid) {
          _log('[gdt-login] ✅ Already logged in (correct account) — skipping stealth sidecar');
          ctx.setResult?.({ success: true, skipped: true, url: page.url() });
          alreadyLoggedIn = true;
          return;
        }
        _log(`[gdt-login] ⚠️  Session inactive or wrong account — will try R2 profile (Check 3)`);
      }

      // ── Check 3: Pull Chrome profile from R2 → launchPersistentContext ──────
      // Runs whenever the session is NOT confirmed valid:
      //   • looksLikeValid=false (no DB flag, no session JSON) — obvious case
      //   • looksLikeValid=true but live check failed — e.g. DB flag stale after reboot
      // Chrome encrypts cookies (AES-CBC). launchPersistentContext lets Chrome
      // decrypt them natively; we then save a Playwright storageState JSON.
      if (!alreadyLoggedIn) {
        _log('[gdt-login] Phase 0 Check 3: Trying R2 Chrome profile (launchPersistentContext)...');
        let r2ProfileValid = false;
        try {
          const { execSync: _es } = await import('node:child_process');
          const syncPy = `${_xiobr}/colab/sync.py`;
          _log('[gdt-login] Phase 0 Check 3: Pulling R2 profile...');
          _es(
            `python3 ${JSON.stringify(syncPy)} --what chrome_profiles --session-id ${JSON.stringify(sessionId)} -q`,
            { encoding: 'utf8', timeout: 120_000, stdio: 'pipe' }
          );

          const { localProfilePath, sessionStatePath: _ssp } =
            await import('file://' + _xiobr + '/src/core/session-manager.mjs');
          const profDir = localProfilePath(sessionId);
          const { existsSync: _ex } = await import('node:fs');

          if (_ex(profDir)) {
            _log(`[gdt-login] Phase 0 Check 3: Profile at ${profDir} — launching patchright`);
            const { chromium: _pr } = await import('patchright');
            const _tmpCtx = await _pr.launchPersistentContext(profDir, {
              headless: false,
              args: [
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
              ],
              ignoreHTTPSErrors: true,
            });
            try {
              const _tmpPage = await _tmpCtx.newPage();
              await _tmpPage.goto('https://myaccount.google.com/', {
                waitUntil: 'domcontentloaded', timeout: 25000,
              }).catch(() => {});
              await humanSleep(2000, 3000);

              const { verifyGoogleSession: _vgs } = await import(
                'file://' + _xiobr + '/src/core/session-verifier.mjs'
              );
              const isR2Valid = await _vgs(_tmpPage, email);
              await _tmpPage.screenshot({ path: `${screenshotDir}/phase0_r2_profile_check.jpg` })
                .catch(() => {});

              if (isR2Valid) {
                _log('[gdt-login] ✅ Phase 0 Check 3: R2 profile valid — saving session JSON');
                const _sessionPath = _ssp(sessionId);
                await _tmpCtx.storageState({ path: _sessionPath });
                _log(`[gdt-login] Phase 0 Check 3: Session JSON saved → ${_sessionPath}`);
                ctx.setResult?.({ success: true, skipped: true, source: 'r2_profile', url: _tmpPage.url() });
                r2ProfileValid = true;
              } else {
                _log('[gdt-login] Phase 0 Check 3: R2 profile cookies are stale — proceeding to Phase 1');
              }
            } finally {
              await _tmpCtx.close().catch(() => {});
            }
          } else {
            _log(`[gdt-login] Phase 0 Check 3: Profile dir not found (${profDir}) — proceeding to Phase 1`);
          }
        } catch (_pe) {
          _log(`[gdt-login] Phase 0 Check 3 error: ${_pe.message?.slice(0, 140)}`);
        }

        if (r2ProfileValid) {
          alreadyLoggedIn = true;
          return;
        }
      }

      // ── Domain-aware eviction — preserve other domain cookies (Tailscale, v0, etc.) ────
      // evictSession() (full JSON delete) is intentionally NOT used here: the state.json
      // may contain valid Tailscale/v0/GitHub cookies that share this session slot.
      // We only strip Google-domain cookies so those services remain authenticated.
      const { evictDomainFromSession, evictLocalProfileCookies } =
        await import('file://' + _xiobr + '/src/core/session-manager.mjs');

      // 1. Strip Google cookies from the shared state.json (preserve all other domains)
      evictDomainFromSession(sessionId, 'google');

      // 2. Strip Google cookies from the UC/Selenium Chrome profile's SQLite Cookies DB.
      //    Stale Google auth rows in Default/Cookies bypass the login form, causing
      //    the profile to navigate directly to myaccount.google.com → verification_failed.
      evictLocalProfileCookies(sessionId, 'google');
    }, { autoScreenshot: false });
  }

  if (alreadyLoggedIn) {
    // Session was valid at phase0 — no stealth signin ran, no cookies changed.
    // Skipping ALL saves: no saveStorageStateFull, no pushToStorage, no R2/Drive/Supabase writes.
    _log('[gdt-login] ✅ Session already valid — no saves needed (cookies unchanged)');
    return { success: true, skipped: true };
  }

  // ── Phase 1: Run stealth sidecar (multi-engine, via stealth-runner) ─────────
  // NOTE: The sidecar runs its own Chrome (uc/camoufox) separately from patchright.
  //       The patchright page (stream-visible) is idle during this phase by design.
  //       Sidecar saves its own CDP screenshots to jobDir at key steps.
  let sidecarResult;
  await ctx.step('phase1_stealth_login', async () => {
    _log('[gdt-login] Phase 1: Launching stealth sidecar...');
    const _signinLock = await acquireSigninLock(sessionId, ctx);
    try {
      sidecarResult = await runStealthSidecar({
        email, password, totp_secret,
        sessionPath, screenshotDir,
        log: _log,
        workflowId:        WORKFLOW_ID,
        domain:            DOMAIN,
        profileDir,
        jobId:             ctx.jobId,   // enables killSidecar() on cancel
        getRankedEngines,
        recordEngineResult,
      });
    } finally {
      await releaseSigninLock(_signinLock, ctx);
    }

    if (!sidecarResult.success) {
      // ── Recoverable failure → HITL pause ──────────────────────────────────
      // When the stealth sidecar fails due to a TOTP/verification issue (not a
      // code bug), pause the job for human intervention instead of hard-failing.
      // The user can resolve the issue (e.g. re-enter TOTP, unblock account) and
      // then resume the job. Hard failures (missing sidecar, no engines) still throw.
      if (sidecarResult.hitl) {
        const _hitlMsg = {
          totp_rejected:        '⚠️  Google rejected the TOTP code during sign-in. The code may have expired or been entered incorrectly. Please verify the TOTP secret is correct, then resume.',
          verification_failed:  '⚠️  Sign-in completed but session verification failed (ended at account/about). Google may have flagged the account. Check the uc_final screenshot and resume when ready.',
          totp_input_not_found: '⚠️  2FA screen appeared but TOTP input field could not be found. The challenge type may have changed. Check screenshots and resume.',
          all_engines_failed:   '⚠️  All stealth login engines failed. Check the step screenshots for the specific challenge and resolve manually.',
        }[sidecarResult.failureType] ?? `⚠️  Google sign-in sidecar failed (${sidecarResult.failureType}).`;

        await ctx.hitl(_hitlMsg, {
          instructions: 'Review the step screenshots in the job folder. Resolve the issue on the Google account, then resume this job.',
          screenshotDir,
        });
        // After resume: sidecar already wrote partial session — re-run sidecar
        sidecarResult = await runStealthSidecar({
          email, password, totp_secret,
          sessionPath, screenshotDir,
          log: _log,
          workflowId: WORKFLOW_ID, domain: DOMAIN,
          profileDir, jobId: ctx.jobId, getRankedEngines, recordEngineResult,
        });
        if (!sidecarResult.success) {
          throw new Error(`All stealth engines failed after HITL resume (${sidecarResult.failureType})`);
        }
        return; // success on second attempt
      }
      throw new Error('All stealth engines failed — check engine logs above');
    }
  }, { autoScreenshot: false });


  // ── Phase 2: Load sidecar session into patchright context ───────────────────
  await ctx.step('phase2_load_cookies', async () => {
    _log('[gdt-login] Phase 2: Loading sidecar session into browser context...');
    const sessionData = JSON.parse(readFileSync(sessionPath, 'utf8'));
    if (sessionData.cookies?.length > 0) {
      await page.context().addCookies(sessionData.cookies);
      _log(`[gdt-login] Loaded ${sessionData.cookies.length} cookies from ${sidecarResult.engine}`);
      // No screenshot here: patchright page hasn't navigated yet — nothing meaningful to capture.
    } else {
      throw new Error('Session file has no cookies — sidecar may have failed silently');
    }
  }, { autoScreenshot: false });

  // ── Phase 3: Verify login in patchright context ─────────────────────────────
  // This phase IS visible in the stream — patchright navigates to myaccount.google.com.
  let finalUrl, success;
  await ctx.step('phase3_verify_login', async () => {
    _log('[gdt-login] Phase 3: Verifying login in patchright context...');
    await page.goto('https://myaccount.google.com/', {
      waitUntil: 'domcontentloaded', timeout: 15000,
    }).catch(() => {});
    await humanSleep(2000, 3000);

    finalUrl         = page.url();
    const finalTitle = await page.title().catch(() => '');
    success          = finalUrl.includes('myaccount.google.com') && !finalTitle.includes('Sign in');

    if (!success) {
      await shot('phase3_verify_FAILED');
      throw new Error(`Verification failed — ended at: ${finalUrl.slice(0, 100)}`);
    }
    _log(`[gdt-login] ✅ Login verified! Engine: ${sidecarResult.engine}, URL: ${finalUrl.slice(0, 80)}`);
    ctx.setResult?.({ success: true, engine: sidecarResult.engine, final_url: finalUrl });

    // ── Save full v2 session state (CDP-based rich capture) ─────────────────
    // NOTE: saveStorageStateFull navigates patchright to each localStorage origin
    // (google.com, accounts.google.com, etc.) and back.
    try {
      const { saveStorageStateFull } = await import('file://' + _xiobr + '/src/core/session-manager.mjs');
      await saveStorageStateFull(sessionId, page);
      _log(`[gdt-login] 💾 Full v2 session state saved (CDP cookies + localStorage + IndexedDB)`);
    } catch (se) {
      _log(`[gdt-login] ⚠️  saveStorageStateFull failed, falling back to basic save: ${se.message}`);
      try {
        const { saveStorageState } = await import('file://' + _xiobr + '/src/core/session-manager.mjs');
        await saveStorageState(sessionId, page.context());
        _log(`[gdt-login] 💾 Fallback: patchright storageState saved to session JSON`);
      } catch (se2) {
        _log(`[gdt-login] ⚠️  Could not save storageState: ${se2.message}`);
      }
    }

    // One definitive screenshot: taken after saveStorageStateFull finishes
    // (which already ends on a Google origin) — no redundant second goto needed.
    await shot('phase3_verified', { waitMs: 400 });

    // ── Mark google service as valid ───────────────────────────────────────
    try {
      const { upsertSessionService } = await import('file://' + _xiobr + '/src/core/db.mjs');
      const { normaliseSessionId }   = await import('file://' + _xiobr + '/src/utils/session-id.mjs');
      upsertSessionService({ session_id: normaliseSessionId(sessionId), service: 'google', account_hint: email, is_valid: 1 });
      _log(`[gdt-login] ✅ session_credentials updated: google=valid`);
    } catch (de) {
      _log(`[gdt-login] ⚠️  session_credentials update failed: ${de.message}`);
    }

    // ── Push session state + chrome_profiles ─────────────────────────────────
    // Uses generalised pushToStorage() helper from session-manager:
    //   Session JSON  -> Supabase (awaited, 15 s) + Drive (background detached)
    //   Chrome profile -> R2 (awaited, 60 s)     + Drive (background detached)
    try {
      const { pushToStorage } = await import('file://' + _xiobr + '/src/core/session-manager.mjs');
      await pushToStorage(sessionId, {
        session: true,
        profile: true,
        awaitPrimary: true,
        log: _log,
      });
    } catch (pe) {
      _log(`[gdt-login] Push skipped: ${pe.message}`);
    }



  }, { autoScreenshot: false });

  return { success: true, engine: sidecarResult.engine, final_url: finalUrl };
}
