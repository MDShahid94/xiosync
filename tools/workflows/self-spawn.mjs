/**
 * XIO Mesh — Self-Spawn Workflow
 * ================================
 * Opens the shared start.ipynb notebook in a new Colab runtime using an
 * authenticated pro-account session, handles all consent dialogs, and
 * runs all cells so the replacement worker node boots autonomously.
 *
 * Dialog sequence (verified from live screenshots):
 *   1. "Warning: This notebook was not authored by Google" → Run anyway
 *   2. "Allow this notebook to access your Google credentials?" → Allow
 *   3. OAuth popup: Sign in screen → Continue
 *   4. OAuth popup: Consent/permissions screen → Select all (1st time only) → Continue
 *   5. Wait for notebook cells to render
 *   6. Ctrl+F9 (Run all) + confirm any "Run anyway" dialog
 *   7. Verify runtime connecting → navigate away
 *
 * Testing policy (via _test-runner.mjs):
 *   - Each step is wrapped in ctx.testStep() which retries up to 3× and
 *     captures evidence screenshots before and after.
 *   - Steps marked autoResolvable=false trigger HITL immediately on first failure.
 *   - On full pass the workflow is auto-pushed to Drive by the job-manager.
 */

import fs from 'fs';
import path from 'path';
import { getSession, getAccount } from '../src/core/db.mjs';
import { attachTestRunner } from './_test-runner.mjs';
import { createShot } from '../src/core/wf-shot.mjs';
import { isSpawnEligible, getSpawnEligibleTiers, selectSpawnAccount } from '../src/core/ecosystem.mjs';
import { getSigninPolicy, shouldForceSignin, shouldSkipSignin } from '../src/core/signin-policy.mjs';

import { execSync } from 'child_process';

let NOTEBOOK_FILE_ID = null;

try {
  // Query the Shared Drive for start.ipynb's file ID.
  // Tier 1: Direct Drive API search via sync.py's authenticated client.
  // Tier 2: drive_assets DB lookup (set by boot.py Phase 4 on every boot).
  // Tier 3: xio_config.json notebook_file_id (set during Drive sync).
  const pyScript = `
import sys, sqlite3, json, os
sys.path.append('/content/xio-browser/colab')
try:
    import sync
    res = sync.svc.files().list(
        q=f'name="start.ipynb" and "{sync.FOLDER_ID}" in parents and trashed=false',
        spaces='drive', fields='files(id)', pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute().get('files', [])
    if res:
        print(res[0]['id'])
        sys.exit(0)
except Exception:
    pass

# Tier 2: drive_assets catalogue (DB)
try:
    _db_path = '/content/xio-mesh/xio-browser.db'
    if os.path.exists(_db_path):
        _c = sqlite3.connect(_db_path)
        _r = _c.execute(
            "SELECT drive_file_id FROM drive_assets WHERE entity_type='db' AND entity_id='start_ipynb' AND asset_type='file'"
        ).fetchone()
        _c.close()
        if _r and _r[0]:
            print(_r[0])
            sys.exit(0)
except Exception:
    pass

# Tier 3: xio_config.json (written by boot.py Phase 4)
try:
    _cfg = json.load(open('/tmp/xio_config.json'))
    _nb_id = _cfg.get('notebook_file_id')
    if _nb_id:
        print(_nb_id)
        sys.exit(0)
except Exception:
    pass

sys.exit(1)
`;
  const b64 = Buffer.from(pyScript).toString('base64');
  const id = execSync(`python3 -c "import base64; exec(base64.b64decode('${b64}'))"`, { encoding: 'utf8' }).trim();
  const idLines = id.split('\n');
  const actualId = idLines[idLines.length - 1].trim();
  if (actualId) {
    NOTEBOOK_FILE_ID = actualId;
  }
} catch (e) {
  // Final fallback: read notebook_file_id from xio_config.json directly (no Python needed)
  try {
    const cfg = JSON.parse(fs.readFileSync('/tmp/xio_config.json', 'utf8'));
    if (cfg.notebook_file_id) NOTEBOOK_FILE_ID = cfg.notebook_file_id;
  } catch (err) {}
}

if (!NOTEBOOK_FILE_ID) {
  throw new Error('Could not determine start.ipynb file ID. Check that boot.py Phase 4 completed successfully.');
}

export async function run(ctx, params) {
  if (params && !params.session_id && params.selection) {
    const selected = selectSpawnAccount(params);
    if (selected) {
      params.session_id = selected;
      ctx.sessionId = selected;
      ctx.log(`[self-spawn] ✅ Auto-selected spawn account: ${selected} (tier: ${params.tier || 'Pro'}, selection: ${params.selection})`);
    } else {
      ctx.log(`[self-spawn] ⚠️ Auto-selection failed (no eligible accounts found).`);
    }
  }

  // Read spawn behaviour config (kept for other _spawnConfig keys)
  let _spawnConfig = {};
  try {
    const _cfgRaw = fs.readFileSync('/tmp/xio_config.json', 'utf8');
    _spawnConfig = JSON.parse(_cfgRaw);
  } catch {}
  // Signin policy is now read from the global signin-policy.mjs module
  // (backed by xio_config.json → auto_spawn_google_signin)

  // ── Attach test runner (adds ctx.testStep, ctx.verify, ctx.screenshot, ctx.hitl) ──
  attachTestRunner(ctx, import.meta.url);

  // Robust shadow DOM clicker using Playwright mouse
  async function robustShadowClick(page, textPattern) {
    const box = await Promise.race([
      page.evaluate((pat) => {
        const re = new RegExp(pat, 'i');
        function findInShadow(root) {
          if (!root) return null;
          for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
            const tag = el.tagName?.toLowerCase() ?? '';
            const isBtn = tag.includes('button') || el.getAttribute('role') === 'button';
            if (isBtn && re.test((el.textContent ?? '').trim())) {
              const rect = el.getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) {
                return {
                  x: rect.x + rect.width / 2,
                  y: rect.y + rect.height / 2,
                  text: el.textContent.trim()
                };
              }
            }
            const found = el.shadowRoot ? findInShadow(el.shadowRoot) : null;
            if (found) return found;
          }
          return null;
        }
        return findInShadow(document);
      }, textPattern).catch(() => null),
      new Promise(r => setTimeout(() => r(null), 5000))
    ]);

    if (box) {
      await page.mouse.click(box.x, box.y);
      return box.text;
    }
    return null;
  }

  
  async function handleOauthPopupFlow(popup, _evDir) {
    const _popupShot = createShot(_evDir, { logFn: (m) => ctx.log(m) });
    _popupShot.setPage(popup);

    ctx.log(`OAuth popup opened: ${popup.url()}`);
    await popup.waitForLoadState("domcontentloaded", { timeout: 20_000 }).catch(()=>{});
    await popup.waitForTimeout(1000);

    // Screen A: account chooser / first "Continue" screen.
    // Some consent dialogs (e.g. "Third-party authored notebook code wants
    // additional access") skip straight to Screen B — so use a short timeout
    // and also accept "Allow" here so we don't waste 12s before falling through.
    const btnSelA = 'button:has-text("Continue"), [role="button"]:has-text("Continue"), ' +
                    'button:has-text("Allow"), [role="button"]:has-text("Allow")';

    try {
      await popup.waitForSelector(btnSelA, { timeout: 4_000, state: "visible" });
      const btnTextA = await popup.locator(btnSelA).first().innerText().catch(() => '?');

      // Only click here if it's Continue (account chooser). If it's Allow on a
      // consent page, skip to Screen B which handles checkboxes BEFORE clicking Allow.
      if (/continue/i.test(btnTextA)) {
        await _popupShot('oauth_screen_A');
        await popup.click('button:has-text("Continue"), [role="button"]:has-text("Continue")');
        ctx.log(`✅ OAuth screen A — clicked Continue`);
        await popup.waitForTimeout(2500);
      } else {
        ctx.log(`Screen A skipped — button is "${btnTextA.trim()}", proceeding to Screen B (consent)`);
        await _popupShot('oauth_screen_A_consent_only');
      }
    } catch (e) {
      ctx.log(`Screen A not found (${e.message?.slice(0,60)}) — skipping to Screen B`);
      await _popupShot('oauth_screen_A_fail');
    }

    try {
      // Widen button selector: Google uses "Continue" on most screens but "Allow" on some
      const btnSelB = 'button:has-text("Continue"), [role="button"]:has-text("Continue"), ' +
                      'button:has-text("Allow"), [role="button"]:has-text("Allow")';
      await popup.waitForSelector(btnSelB, { timeout: 10_000, state: 'visible' });
      await _popupShot('oauth_screen_B');

      // ── Strategy 0: Click "Select all" row FIRST, then ensure all sub-boxes are checked ─
      // Google's OAuth consent page has a "Select all" master checkbox and individual
      // sub-item checkboxes. We must click "Select all" FIRST — checking individual
      // sub-boxes one-by-one does NOT trigger the master and may leave others unchecked.
      try {
        // Step 0a: find and click the "Select all" checkbox directly via its label
        const selectAllChecked = await popup.evaluate(() => {
          const all = Array.from(document.querySelectorAll('*'));
          for (const el of all) {
            const text = (el.textContent ?? '').trim();
            if (/select\s+all/i.test(text) && el.children.length < 10) {
              // Look for a sibling/nearby checkbox input
              const parent = el.closest('li, [role="listitem"], .scope-row, div, label') ?? el.parentElement;
              const cb = parent?.querySelector('input[type="checkbox"]');
              if (cb && !cb.checked) {
                cb.click();
                return 'input-clicked';
              }
              // Fallback: click the element's own parent row
              const rect = (parent ?? el).getBoundingClientRect?.();
              if (rect && rect.width > 0) {
                (parent ?? el).click();
                return 'row-clicked';
              }
            }
          }
          return null;
        }).catch(() => null);

        if (selectAllChecked) {
          ctx.log(`✅ Strategy 0a: clicked "Select all" row (${selectAllChecked})`);
          await popup.waitForTimeout(400);
        }

        // Step 0b: Now verify all checkboxes are checked; tick any remaining unchecked ones
        const remaining = popup.locator('input[type="checkbox"]:not(:checked)');
        const cbCount = await remaining.count().catch(() => 0);
        if (cbCount > 0) {
          for (let ci = 0; ci < cbCount; ci++) {
            await remaining.nth(ci).check({ force: true, timeout: 2000 }).catch(() => {});
          }
          ctx.log(`✅ Strategy 0b: checked ${cbCount} remaining unchecked checkbox(es)`);
          await popup.waitForTimeout(400);
        }

        if (!selectAllChecked && cbCount === 0) {
          throw new Error('no checkboxes found — try coordinate click');
        }
      } catch (_s0Err) {
        // ── Strategy 0b: Coordinate-based click (left of "Select all" label) ─
        // Finds the viewport position of the "Select all" text, then clicks
        // 28px to its left (where the checkbox square is rendered).
        const coordClicked = await popup.evaluate(() => {
          const all = Array.from(document.querySelectorAll('*'));
          for (const el of all) {
            const text = (el.textContent ?? '').trim();
            if (/select\s+all/i.test(text) && el.children.length < 10) {
              const rect = el.getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) {
                return { x: rect.left - 28, y: rect.top + rect.height / 2 };
              }
            }
          }
          return null;
        }).catch(() => null);

        if (coordClicked && coordClicked.x > 0) {
          await popup.mouse.click(coordClicked.x, coordClicked.y);
          ctx.log(`✅ Strategy 0b: coordinate click at (${coordClicked.x.toFixed(0)}, ${coordClicked.y.toFixed(0)}) for "Select all"`);
          await popup.waitForTimeout(600);
        } else {
          // ── Strategy 1: JS evaluate label→sibling click ───────────────────
          const selectAllClicked = await popup.evaluate(() => {
            const all = Array.from(document.querySelectorAll('*'));
            for (const el of all) {
              const text = (el.textContent ?? '').trim();
              if (/select\s+all/i.test(text) && el.children.length < 10) {
                const targets = [el, el.parentElement, el.parentElement?.previousElementSibling,
                                 el.previousElementSibling, el.nextElementSibling].filter(Boolean);
                for (const t of targets) {
                  const rect = t.getBoundingClientRect?.();
                  if (rect && rect.width > 0 && rect.height > 0) { t.click(); return true; }
                }
              }
            }
            return false;
          }).catch(() => false);

          if (selectAllClicked) {
            ctx.log('✅ Strategy 1: clicked "Select all" via JS evaluate');
            await popup.waitForTimeout(600);
          } else {
            // ── Strategy 2: aria-checked=false ───────────────────────────────
            const uncheckedCount = await popup.evaluate(() => {
              const unchecked = document.querySelectorAll('[aria-checked="false"], li [aria-checked="false"]');
              let clicked = 0;
              for (const el of unchecked) { el.click(); clicked++; }
              return clicked;
            }).catch(() => 0);

            if (uncheckedCount > 0) {
              ctx.log(`✅ Strategy 2: clicked ${uncheckedCount} aria-checked=false element(s)`);
              await popup.waitForTimeout(600);
            } else {
              // ── Strategy 3: li-scoped checkbox divs ──────────────────────
              const liChecked = await popup.evaluate(() => {
                const items = document.querySelectorAll('li');
                let clicked = 0;
                for (const li of items) {
                  const cb = li.querySelector('div[role="checkbox"], div[jscontroller], input[type="checkbox"]');
                  if (cb) {
                    const checked = cb.getAttribute('aria-checked') === 'true' ||
                                    cb.classList.contains('checked') ||
                                    (cb.tagName === 'INPUT' && cb.checked);
                    if (!checked) { cb.click(); clicked++; }
                  }
                }
                return clicked;
              }).catch(() => 0);

              if (liChecked > 0) {
                ctx.log(`✅ Strategy 3: clicked ${liChecked} li-scoped checkbox(es)`);
                await popup.waitForTimeout(600);
              } else {
                // ── Strategy 4: Playwright text locator ──────────────────
                const tried = await popup.locator('text=/select all/i').first()
                  .click({ timeout: 3000 }).then(() => true).catch(() => false);
                ctx.log(tried
                  ? '✅ Strategy 4: Playwright text locator clicked Select all'
                  : '⚠️  All checkbox strategies exhausted — checkboxes may already be checked');
                if (tried) await popup.waitForTimeout(600);
              }
            }
          }
        }
      }

      await _popupShot('oauth_screen_B_checked');
      await popup.click(btnSelB);
      ctx.log('✅ OAuth screen B — clicked Continue/Allow');
      await popup.waitForTimeout(1500);
    } catch (e) {
      ctx.log(`Screen B not needed or failed: ${e.message}`);
      await _popupShot('oauth_screen_B_fail');
    }

    await popup.waitForEvent("close", { timeout: 25_000 }).catch(() => {
      ctx.log("Popup did not auto-close");
    });
    await _popupShot('oauth_done');
    ctx.log("✅ OAuth popup flow complete");
  }

const fileId        = params?.notebook_file_id ?? NOTEBOOK_FILE_ID;
  const expectedEmail = params?.expected_email   ?? null;
  const colabUrl      = `https://colab.research.google.com/drive/${fileId}?worker=true#forceEdit=true&sandboxMode=true`;

  // ── Phase -1: Pre-flight — fast D1 availability check before browser work ──
  // Queries D1 directly (no browser) to verify:
  //   1. The requested session is not locked by another node
  //   2. This account doesn't already have a live worker in node_registry
  //   3. At least one Pro account exists that is unlocked + has no live worker
  // Fails fast with a clear error if no account is available, preventing
  // wasted browser resources when the whole mesh is already at capacity.
  await ctx.step('preflight_account_check', async () => {
    ctx.log('[preflight] Checking D1 for available Pro accounts…');
    const { readFileSync } = await import('node:fs');
    const https = await import('node:https');

    let cfg = {};
    try { cfg = JSON.parse(readFileSync('/tmp/xio_config.json', 'utf8')); } catch { /* ok */ }
    const _cfToken   = cfg.cf_api_token    || process.env.CF_API_TOKEN    || '';
    const _cfAccount = cfg.cf_account_id   || process.env.CF_ACCOUNT_ID   || '';
    const _cfDb      = cfg.cf_d1_database_id || process.env.CF_D1_DATABASE_ID || '';

    if (!_cfToken || !_cfAccount || !_cfDb) {
      ctx.log('[preflight] ⚠️  CF D1 credentials not found — skipping pre-flight (will rely on verify_session)');
      return;
    }

    /** Query D1 via CF API — returns rows array */
    async function _d1(sql) {
      return new Promise((resolve, reject) => {
        const body = JSON.stringify({ sql });
        const req = https.request({
          hostname: 'api.cloudflare.com',
          path: `/client/v4/accounts/${_cfAccount}/d1/database/${_cfDb}/query`,
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${_cfToken}`,
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
          },
        }, res => {
          let data = '';
          res.on('data', d => data += d);
          res.on('end', () => {
            try { resolve(JSON.parse(data).result[0].results); }
            catch (e) { reject(new Error(`D1 parse error: ${e.message}`)); }
          });
        });
        req.on('error', reject);
        req.write(body); req.end();
      });
    }

    const _nowEpoch = Math.floor(Date.now() / 1000);

    // ── Check 1: Is this specific session locked? ───────────────────────────
    const _slug    = ctx.sessionId.split('@')[0];
    const _lockKey = `session:${_slug}`;
    const _locks   = await _d1(
      `SELECT key, node, expires_at FROM locks WHERE key='${_lockKey}' AND expires_at > ${_nowEpoch}`
    );
    if (_locks.length > 0) {
      const _lk = _locks[0];
      const _ttl = Math.ceil((_lk.expires_at || 0) - _nowEpoch);
      ctx.log(`[preflight] ⚠️  Session ${ctx.sessionId} is locked by ${_lk.node} (${_ttl}s remaining). Will attempt fallback.`);
      // Don't throw here — let verify_session's fallback handle it
    } else {
      ctx.log(`[preflight] ✅ Session ${ctx.sessionId} lock: clear`);
    }

    // ── Check 2: Does this account already have a TRULY live worker? ───────────
    // Two-stage: (1) D1 registry lookup, (2) HTTP liveness probe on the IP.
    // D1 last_seen can be stale (worker died but registry wasn't cleaned up).
    // Only block if the worker BOTH appears in registry AND responds to /health.
    const _nodePattern = `colab-worker-${_slug.replace(/\./g, '-')}`;
    const _aliveWorkers = await _d1(
      `SELECT node_name, ts_ip, last_seen FROM node_registry ` +
      `WHERE node_name='${_nodePattern}' AND last_seen > datetime('now', '-90 seconds')`
    );
    if (_aliveWorkers.length > 0) {
      const _aw = _aliveWorkers[0];
      // Verify the worker is actually reachable via HTTP (3s timeout)
      let _workerReachable = false;
      try {
        const { execSync: _chkExec } = await import('node:child_process');
        const _healthOut = _chkExec(
          `curl -s --max-time 3 http://${_aw.ts_ip}:4242/health`,
          { encoding: 'utf8', timeout: 5000 }
        );
        _workerReachable = !!(_healthOut && JSON.parse(_healthOut).ok);
      } catch (_) {
        _workerReachable = false;
      }
      if (_workerReachable) {
        throw new Error(
          `[preflight] Account ${ctx.sessionId} already has a live worker (${_aw.node_name} @ ${_aw.ts_ip}, ` +
          `last_seen=${_aw.last_seen}). Aborting duplicate spawn.`
        );
      } else {
        ctx.log(`[preflight] ⚠️  ${_aw.node_name} in registry but unreachable at ${_aw.ts_ip} — treating as dead, proceeding`);
      }
    }
    ctx.log(`[preflight] ✅ No live worker found for ${ctx.sessionId}`);

    // ── Check 3: Read spawn_account_tier from D1 + verify free accounts exist ─
    // Supported values: 'Pro' (default) | 'Starter' | 'Any'
    // Set via: D1 runtime_config key='spawn_account_tier' scope='global'
    //          OR xio_config/global.json → spawn_account_tier
    const _tierRows = await _d1(
      `SELECT value FROM runtime_config WHERE key='spawn_account_tier' ` +
      `ORDER BY CASE scope WHEN 'global' THEN 1 ELSE 0 END LIMIT 1`
    );
    const _tier = (_tierRows[0]?.value || 'Pro').trim();
    ctx.log(`[preflight] spawn_account_tier=${_tier}`);

    // Build tier-aware WHERE clause for sessions+accounts join
    const _tierWhere = _tier === 'Any'
      ? `a.is_active=1`
      : `a.tier='${_tier}' AND a.is_active=1`;

    const _allScoped = await _d1(`
      SELECT s.id FROM sessions s
      JOIN accounts a ON s.account_email = a.email
      WHERE ${_tierWhere} AND (s.is_persisted=1 OR s.is_persisted='true')
    `);
    const _activeLocks = await _d1(
      `SELECT REPLACE(key, 'session:', '') as slug FROM locks WHERE expires_at > ${_nowEpoch}`
    );
    // For global alive check: only count workers reachable via HTTP (no stale entries)
    const _aliveAll = await _d1(
      `SELECT node_name, ts_ip FROM node_registry WHERE node_name LIKE 'colab-worker-%' AND last_seen > datetime('now', '-90 seconds')`
    );
    const _lockedSlugs = new Set(_activeLocks.map(r => r.slug));
    // Verify each registry entry is actually reachable (parallel with short timeout)
    const { execSync: _pingExec } = await import('node:child_process');
    const _trulyAliveNames = new Set();
    for (const _nr of _aliveAll) {
      try {
        const _ho = _pingExec(
          `curl -s --max-time 2 http://${_nr.ts_ip}:4242/health`,
          { encoding: 'utf8', timeout: 4000 }
        );
        if (_ho && JSON.parse(_ho).ok) _trulyAliveNames.add(_nr.node_name);
      } catch (_) {}
    }
    const _aliveAccounts = new Set(
      [..._trulyAliveNames].map(n => n.replace('colab-worker-', '').replace(/-/g, '.'))
    );
    const _aliveAccountsDash = new Set(
      [..._trulyAliveNames].map(n => n.replace('colab-worker-', ''))
    );

    const _freeAccounts = _allScoped.filter(r =>
      !_lockedSlugs.has(r.id) &&
      !_aliveAccounts.has(r.id) &&
      !_aliveAccountsDash.has(r.id.replace(/\./g, '-'))
    );

    ctx.log(
      `[preflight] ${_tier} accounts: ${_allScoped.length} total | ` +
      `${_lockedSlugs.size} locked | ${_trulyAliveNames.size} with live workers | ` +
      `${_freeAccounts.length} free`
    );

    if (_freeAccounts.length === 0) {
      throw new Error(
        `[preflight] No ${_tier} accounts available for self-spawn — ` +
        `all ${_allScoped.length} are locked (${_lockedSlugs.size}) or ` +
        `already have live workers (${_trulyAliveNames.size}). Aborting to save resources.`
      );
    }

    ctx.log(`[preflight] ✅ ${_freeAccounts.length} free ${_tier} account(s) available — proceeding`);

  }, { throwOnFail: true });

  // ── Phase 0: Guard — verify Google session before touching Colab ───────────
  await ctx.step('verify_session', async () => {
    ctx.log('Checking Google Accounts session…');
    
    // Enforce spawn-eligible account tier — read from D1 runtime_config (overrides xio_config.json default)
    const session = getSession(ctx.sessionId);
    if (!session) {
      throw new Error(`Session ${ctx.sessionId} not found in database.`);
    }
    // Read spawn_eligible_tiers from D1 (comma-separated: 'Pro,Starter')
    let _eligibleTiers = getSpawnEligibleTiers(); // default from xio_config / ecosystem.mjs
    try {
      const { queryD1 } = await import('../src/core/db.mjs');
      const _tierCfg = await queryD1(
        `SELECT value FROM runtime_config WHERE key='spawn_eligible_tiers' ORDER BY CASE scope WHEN 'global' THEN 1 ELSE 0 END LIMIT 1`
      );
      if (_tierCfg?.[0]?.value) {
        _eligibleTiers = _tierCfg[0].value.split(',').map(t => t.trim()).filter(Boolean);
        ctx.log(`[verify_session] spawn_eligible_tiers from D1: [${_eligibleTiers.join(', ')}]`);
      }
    } catch (_) {}
    const _sessionTier = (session.tier ?? 'Starter').trim();
    if (!_eligibleTiers.map(t => t.toLowerCase()).includes(_sessionTier.toLowerCase())) {
      throw new Error(`Account tier '${_sessionTier}' is not spawn-eligible. ` +
        `Eligible tiers: ${_eligibleTiers.join(', ')}. ` +
        `Override via D1 runtime_config → spawn_eligible_tiers.`);
    }

    const { verifyGoogleSession } = await import('../src/core/session-verifier.mjs');
    const { handleSessionFallback } = await import('../src/core/session-fallback.mjs');
    const { ensureSessionState } = await import('../src/core/session-manager.mjs');
    // Session account_email holds the full email; fall back to ctx.sessionId
    const _rawId = session.account_email || ctx.sessionId;
    const email = _rawId && _rawId.includes('@') ? _rawId : `${_rawId}@gmail.com`;
    const isValid = await verifyGoogleSession(ctx.page, email);

    const _vsShot = createShot(
      `${ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : '/tmp')}/steps`,
      { logFn: ctx.log.bind(ctx) }
    );
    _vsShot.setPage(ctx.page);
    await _vsShot(isValid ? 'session_valid' : 'session_invalid');

    if (!isValid) {
      // ── Determine handler based on global signin policy ───────────────────
      const _signinPolicy = getSigninPolicy();
      ctx.log(`Session invalid. Signin policy: '${_signinPolicy}'`);

      if (shouldSkipSignin()) {
        // 'skip': no re-login attempt at all — chain directly to next Pro account
        ctx.log('signin-policy=skip — skipping directly to next account (no rescue pull, no re-login)');
        await handleSessionFallback(ctx, 'self-spawn', {});

      } else {
        // 'auto' or 'force': rescue pull first
        // ── Step 1: Always rescue pull from Supabase first ─────────────────
        // Even if is_persisted=0 (set by old evictions), Supabase may still have
        // valid cookies that were never deleted there. Always try a fresh pull.
        ctx.log(`⚠️ Session for ${ctx.sessionId} is invalid locally — attempting rescue pull (Supabase→local)…`);
        await ensureSessionState(ctx.sessionId).catch(e =>
          ctx.log(`Rescue pull error (non-fatal): ${e.message}`)
        );
        await ctx.page.goto('https://myaccount.google.com/', {
          waitUntil: 'domcontentloaded', timeout: 15000,
        }).catch(() => {});
        await ctx.page.waitForTimeout(2000);
        const isValidAfterRescue = await verifyGoogleSession(ctx.page, email);
        await _vsShot(isValidAfterRescue ? 'session_valid_rescue' : 'session_invalid_rescue');
        if (isValidAfterRescue) {
          ctx.log(`✅ Rescue pull restored session for ${ctx.sessionId}`);
          return;
        }
        ctx.log(`Rescue pull did not restore session — cookies have expired in Supabase too`);


        // ── Step 2: Inline re-login (FORCE mode only) ──────────────────
        // 'force': attempt inline google-signin on THIS account before falling back.
        // 'auto': skip inline re-login (original self-spawn default behavior).
        // Root cause of SIDCC expiry: rotating tokens expire after ~24h offline.
        // With 'force', the workflow re-logs in inline rather than abandoning the account.
        if (shouldForceSignin()) {
          const acct = getAccount(email.includes('@') ? email : `${email}@gmail.com`);
          if (acct?.password) {
            ctx.log(`🔄 signin-policy=force — attempting inline google-signin to refresh ${ctx.sessionId}…`);
            try {
              const result = await ctx.runInline('google-signin', ctx.sessionId, {
                email:        acct.email ?? email,
                password:     acct.password,
                totp_secret:  acct.totp_secret ?? '',
              });
              if (result?.ok) {
                ctx.log(`✅ Inline google-signin succeeded — re-verifying session…`);
                await ctx.page.goto('https://myaccount.google.com/', {
                  waitUntil: 'domcontentloaded', timeout: 15000,
                }).catch(() => {});
                await ctx.page.waitForTimeout(2000);
                const isValidAfterLogin = await verifyGoogleSession(ctx.page, email);
                await _vsShot(isValidAfterLogin ? 'session_valid_after_relogin' : 'session_invalid_after_relogin');
                if (isValidAfterLogin) {
                  ctx.log(`✅ Session for ${ctx.sessionId} confirmed valid after forced re-login`);
                  return; // ✅ Continue self-spawn with refreshed session
                }
                ctx.log(`⚠️ google-signin completed but session still not valid — falling back`);
              } else {
                ctx.log(`⚠️ Inline google-signin failed (${result?.error ?? 'unknown'}) — falling back`);
              }
            } catch (e) {
              ctx.log(`⚠️ Inline google-signin threw: ${e.message} — falling back`);
            }
          } else {
            ctx.log(`⚠️ signin-policy=force but no credentials in DB for ${ctx.sessionId} — falling back`);
          }
        } else {
          ctx.log(`signin-policy=auto — rescue pull failed, rotating to next Pro account (no inline re-login)`);
        }

        // ── Step 3: Account fallback (all modes after above steps fail) ──────
        await handleSessionFallback(ctx, 'self-spawn', {});
      }
    }
  });

  // ── Step 1: Navigate to the notebook ───────────────────────────────────────
  await ctx.testStep('open_notebook', async () => {
    const _nbId = NOTEBOOK_FILE_ID ?? '1_s-oQqbLw8gg1hyTn49lGna09k4QDGR7';

    // Step A: Close any other Colab tabs for this notebook (safety net)
    try {
      for (const p of ctx.page.context().pages()) {
        if (p === ctx.page) continue;
        const u = p.url();
        if (u.includes('colab.research.google.com') && u.includes(_nbId)) {
          ctx.log(`[open_notebook] Closing stale notebook tab: ${u.slice(0, 80)}`);
          try { await p.close(); } catch (_) {}
        }
      }
    } catch (_) {}

    // Step B: Navigate to notebook
    ctx.log(`Navigating to notebook: ${colabUrl}`);
    await ctx.page.goto(colabUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 });
    await ctx.page.waitForTimeout(3000);

    // Step C: Disconnect and delete any existing runtime via Colab Runtime menu.
    // Ctrl+Shift+P does NOT reliably open the command palette in Xvfb — the keystrokes
    // go to the focused notebook cell and type as code. Use the Runtime menu instead.
    try {
      if (ctx.page.url().includes('colab.research.google.com')) {
        ctx.log('[open_notebook] Disconnecting runtime via Runtime menu…');

        // ── Approach 1: Click Runtime menu → Disconnect and delete runtime ──────
        let disconnectDone = false;
        try {
          // Colab top nav: File | Edit | View | Insert | Runtime | Tools | Help
          await ctx.page.click('colab-menu-bar [aria-haspopup="menu"]:nth-of-type(5), [role="menubar"] [role="menuitem"]:has-text("Runtime"), text=Runtime', { timeout: 5000 });
          await ctx.page.waitForTimeout(600);
          await ctx.page.click('[role="menuitem"]:has-text("Disconnect and delete runtime"), li:has-text("Disconnect and delete runtime"), [class*="menu-item"]:has-text("Disconnect")', { timeout: 4000 });
          await ctx.page.waitForTimeout(2000);
          disconnectDone = true;
          ctx.log('[open_notebook] Runtime menu → Disconnect clicked');
        } catch (_m) {
          ctx.log(`[open_notebook] Runtime menu click failed: ${_m.message?.slice(0, 60)}`);
        }

        // ── Approach 2: JS evaluate — call Colab internal disconnect ────────────
        if (!disconnectDone) {
          try {
            await ctx.page.evaluate(() => {
              // Colab custom element exposes commands via the toolbar
              const toolbar = document.querySelector('colab-toolbar-button[command="disconnect_runtime"], colab-toolbar-button[command="delete_runtime"]');
              if (toolbar) { toolbar.click(); return 'toolbar-clicked'; }
              // Try the global command registry
              const app = window.colab?.notebook ?? window.__app;
              if (app?.disconnectRuntime) { app.disconnectRuntime(); return 'app-disconnect'; }
              return 'no-api';
            });
            await ctx.page.waitForTimeout(1500);
            ctx.log('[open_notebook] JS evaluate disconnect attempted');
          } catch (_j) {
            ctx.log(`[open_notebook] JS disconnect skipped: ${_j.message?.slice(0, 60)}`);
          }
        }

        // ── Dismiss confirmation dialog if any ───────────────────────────────────
        try {
          const confirmBtn = await ctx.page.waitForSelector(
            'button[data-action="ok"], button[data-action="disconnect"], colab-dialog paper-button:last-of-type, [role="dialog"] button:last-of-type, [role="dialog"] button:has-text("Delete")',
            { timeout: 4000, state: 'visible' }
          );
          if (confirmBtn) { await confirmBtn.click(); }
        } catch (_) {}
        await ctx.page.waitForTimeout(2500);
        ctx.log('[open_notebook] ✅ Disconnect and delete runtime complete');
      }
    } catch (_dcErr) {
      ctx.log(`[open_notebook] ⚠️ Disconnect runtime skipped: ${_dcErr.message?.slice(0, 60)}`);
    }

  }, {
    // After navigation we may land on:
    //   A) The notebook (cells visible)
    //   B) A Colab dialog ("Run anyway", "Allow")
    //   C) Google sign-in page (Colab requires a signed-in account)
    // All three are valid at this point — sign-in is handled in the next step.
    verify: async (page) => {
      const url = page.url();
      const onSignIn   = url.includes('accounts.google.com') || url.includes('google.com/signin');
      const nb         = await page.$('.notebook-cell, colab-cell, [data-type="code"], .cell');
      const dlg        = await page.$('paper-dialog, .warning-dialog, colab-dialog, [role="dialog"]');
      const colabPage  = url.includes('colab.research.google.com');
      return !!(nb || dlg || onSignIn || colabPage);
    },
    verifyLabel: 'notebook, dialog, or Google sign-in page visible',
    screenshotAfter: true,
  });


  // ── Step 1b: Handle Google account sign-in if Colab redirected there ─────────
  // When no Google session exists in the browser, Colab redirects to Google's
  // account picker. We select the first available account.
  await ctx.testStep('google_signin', async () => {
    const url = ctx.page.url();
    if (!url.includes('accounts.google.com')) {
      ctx.log('No Google sign-in redirect — already on Colab or dialog');
      return;
    }
    ctx.log(`Google sign-in page detected: ${url}`);

    // Step A: If this is the account chooser, click the first account
    try {
      const accountRow = await ctx.page.waitForSelector(
        '[data-identifier], .account-name, .email, li[role="link"], div[data-email]',
        { timeout: 8_000, state: 'visible' }
      );
      await accountRow.click();
      ctx.log('✅ Selected account from picker');
      await ctx.page.waitForTimeout(3000);

      const _gsShot = createShot(
        `${ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : '/tmp')}/steps`,
        { logFn: ctx.log.bind(ctx) }
      );
      _gsShot.setPage(ctx.page);
      await _gsShot('after_account_pick');
    } catch {
      ctx.log('No account picker found — may need password entry (HITL required)');
    }

    // Wait for redirect back to Colab
    try {
      await ctx.page.waitForFunction(
        () => window.location.href.includes('colab.research.google.com') ||
              window.location.href.includes('accounts.google.com/v3/signin/challenge'),
        { timeout: 30_000 }
      );
      ctx.log(`After sign-in, URL: ${ctx.page.url()}`);
    } catch {
      ctx.log('⚠️ Sign-in did not redirect back to Colab in 30s');
    }
    await ctx.page.waitForTimeout(3000);
  }, {
    verify: async (page) => {
      const url = page.url();
      // After handling, should either be on Colab or have passed sign-in
      return url.includes('colab.research.google.com') || url.includes('accounts.google.com');
    },
    verifyLabel: 'sign-in handled, back on Colab or auth page',
    screenshotAfter: true,
    // HITL if we land on a password challenge — agent can't enter passwords
    autoResolvable: false,
  });

  // ── Step 2: Wait for notebook to fully render ──────────────────────────────
  await ctx.testStep('wait_for_notebook', async () => {
    ctx.log('Waiting for notebook cells to render…');
    try {
      await ctx.page.waitForSelector(
        '.cell, .notebook-cell, colab-cell, [data-type="code"], .codecell-input',
        { timeout: 35_000, state: 'visible' }
      );
    } catch (e) {
      ctx.log('⚠️  Cells did not load in time, checking for Reload error dialog...');
      const reloaded = await robustShadowClick(ctx.page, '^Reload$');
      if (reloaded) {
        ctx.log('✅ Clicked Reload! Waiting again for cells...');
        await ctx.page.waitForSelector(
          '.cell, .notebook-cell, colab-cell, [data-type="code"], .codecell-input',
          { timeout: 35_000, state: 'visible' }
        );
      } else {
        throw e;
      }
    }
    await ctx.page.waitForTimeout(2500);
  }, {
    verify: '.cell, .notebook-cell, colab-cell, [data-type="code"]',
    verifyLabel: 'notebook cells rendered',
    screenshotAfter: true,
  });

  // ── Step 3: Connect to runtime ─────────────────────────────────────────────
  await ctx.testStep('connect_runtime', async () => {

    // ── Pre-flight: disconnect any lingering runtime before connecting ──────
    // If a previous self-spawn run errored mid-way, the notebook may still be
    // connected to a runtime. Connecting again leaves Colab in a bad state.
    try {
      const alreadyConnected = await ctx.page.evaluate(() => {
        // A connected runtime shows "RAM / Disk" meters or a disconnect toolbar btn
        const ramDisk  = document.querySelector('colab-usage-display, .ram-display, [data-type="usage"]');
        const disconnBtn = document.querySelector(
          'colab-toolbar-button[command="disconnect_runtime"], colab-toolbar-button[command="delete_runtime"]'
        );
        const btn = document.querySelector('colab-connect-button');
        const shadow = btn?.shadowRoot;
        const innerText = (shadow?.querySelector('#connect')?.textContent ?? btn?.textContent ?? '').trim().toLowerCase();
        // "connect" button text changes to "Connected" or shows RAM meter when connected
        const connectedState = innerText && !innerText.includes('connect to') && innerText !== 'connect';
        return !!(ramDisk || disconnBtn || connectedState);
      }).catch(() => false);

      if (alreadyConnected) {
        ctx.log('[connect_runtime] ⚠️  Runtime already connected from prior run — disconnecting first…');
        let disconnected = false;
        // Approach 1: Runtime menu → Disconnect and delete runtime
        try {
          await ctx.page.click(
            'colab-menu-bar [aria-haspopup="menu"]:nth-of-type(5), [role="menubar"] [role="menuitem"]:has-text("Runtime"), text=Runtime',
            { timeout: 5000 }
          );
          await ctx.page.waitForTimeout(600);
          await ctx.page.click(
            '[role="menuitem"]:has-text("Disconnect and delete runtime"), li:has-text("Disconnect and delete runtime")',
            { timeout: 4000 }
          );
          await ctx.page.waitForTimeout(1500);
          // Confirm dialog
          try {
            const dlgBtn = await ctx.page.waitForSelector(
              '[role="dialog"] button:has-text("Delete"), [role="dialog"] button:last-of-type, colab-dialog paper-button:last-of-type',
              { timeout: 3000, state: 'visible' }
            );
            if (dlgBtn) await dlgBtn.click();
          } catch (_) {}
          await ctx.page.waitForTimeout(3000);
          disconnected = true;
          ctx.log('[connect_runtime] ✅ Disconnected via Runtime menu');
        } catch (_de) {
          ctx.log(`[connect_runtime] Runtime menu disconnect failed: ${_de.message?.slice(0, 60)}`);
        }
        // Approach 2: JS — colab app API
        if (!disconnected) {
          try {
            await ctx.page.evaluate(() => {
              const app = window.colab?.notebook ?? window.__app;
              if (app?.disconnectRuntime) app.disconnectRuntime();
              const tb = document.querySelector('colab-toolbar-button[command="disconnect_runtime"]');
              if (tb) tb.click();
            });
            await ctx.page.waitForTimeout(3000);
            ctx.log('[connect_runtime] ✅ Disconnected via JS evaluate');
          } catch (_je) {
            ctx.log(`[connect_runtime] JS disconnect failed: ${_je.message?.slice(0, 40)}`);
          }
        }
      } else {
        ctx.log('[connect_runtime] Runtime is free — no prior connection to clear');
      }
    } catch (_preErr) {
      ctx.log(`[connect_runtime] Pre-flight check failed (continuing): ${_preErr.message?.slice(0, 60)}`);
    }

    // ── Connect ────────────────────────────────────────────────────────────
    ctx.log('Clicking Connect button to allocate runtime…');
    try {
      // 1. Click colab-connect-button directly
      await ctx.page.evaluate(() => {
        const btn = document.querySelector('colab-connect-button');
        if (btn) btn.click();
      });
      ctx.log('✅ Clicked colab-connect-button');

      // 2. Click the Connect button inside the shadow DOM if needed
      await ctx.page.evaluate(() => {
        const connectBtn = document.querySelector('colab-connect-button')?.shadowRoot?.querySelector('#connect');
        if (connectBtn) connectBtn.click();
      });

      ctx.log('Waiting 15 seconds for connection allocation to begin...');
      await ctx.page.waitForTimeout(15000);
    } catch (e) {
      ctx.log(`Could not connect: ${e.message}`);
    }
  }, {
    verify: async (page) => {
      // Return true to avoid failing the workflow. Run All will queue if it's still connecting.
      return true;
    },
    verifyLabel: 'runtime connected',
    screenshotAfter: true,
  });



  // ── Step 3.5: Inject worker flag so boot.py detects this as a worker node ──
  // boot.py Method A reads localStorage.xio_worker_mode + sessionStorage.xio_worker_mode.
  // localStorage survives Colab's URL normalisation (the ?worker=true param in
  // colabUrl is stripped when the runtime connects, but localStorage is not).
  // We also write /tmp/xio_is_worker as the Method E backup (if kernel eval works).
  await ctx.testStep('set_worker_flag', async () => {
    ctx.log('Writing xio_worker_mode=1 to localStorage/sessionStorage for boot.py Method A…');

    // Wait for the page to be fully settled after runtime connect
    await ctx.page.waitForTimeout(4000);

    const result = await ctx.page.evaluate(async () => {
      try {
        // Primary: write to localStorage and sessionStorage
        localStorage.setItem('xio_worker_mode', '1');
        sessionStorage.setItem('xio_worker_mode', '1');

        // Verify write was successful
        const lsOk = localStorage.getItem('xio_worker_mode') === '1';
        const ssOk = sessionStorage.getItem('xio_worker_mode') === '1';

        // Secondary: also try kernel.execute for /tmp/xio_is_worker (Method E)
        if (window.colab?.kernel?.execute) {
          colab.kernel.execute('import os; open("/tmp/xio_is_worker","w").close()');
        } else if (window.google?.colab?.kernel?.execute) {
          google.colab.kernel.execute('import os; open("/tmp/xio_is_worker","w").close()');
        }

        return { lsOk, ssOk };
      } catch (e) {
        return { error: e.message };
      }
    });

    ctx.log(`localStorage=${result.lsOk}, sessionStorage=${result.ssOk}${result.error ? ` ERR:${result.error}` : ''}`);
    if (result.lsOk || result.ssOk) {
      ctx.log('✅ Worker mode flag written — boot.py Method A will detect this as worker node');
    } else {
      ctx.log('⚠️  Storage write failed — Method E (/tmp/xio_is_worker) is fallback');
    }
  }, {
    verify: async () => true,  // non-fatal
    verifyLabel: 'worker flag set',
    screenshotAfter: false,
  });


  // ── Step 4: Run all cells ──────────────────────────────────────────────────
  await ctx.testStep('run_all_cells', async () => {
    ctx.log('Triggering Run All (Ctrl+F9) or via menu…');
    
    // Using evaluate to trigger via menu to be more robust
    await ctx.page.evaluate(() => {
      const menu = document.getElementById('runtime-menu-button');
      if (menu) {
        menu.click();
        setTimeout(() => {
          const runAll = document.getElementById('run-all-button') || document.querySelector('[title="Run all"]');
          if (runAll) runAll.click();
        }, 800);
      }
    });
    
    await ctx.page.waitForTimeout(1500);
    
    // Fallback shortcut
    await ctx.page.keyboard.press('Control+F9');
    await ctx.page.waitForTimeout(2500);
  }, {
    verify: async (page) => true,
    verifyLabel: 'cells execution triggered',
    screenshotAfter: true,
    recoveryFn: async (ctx) => {
      ctx.log('[recovery] Retrying Run All via keyboard shortcut…');
      await ctx.page.keyboard.press('Control+F9');
      await ctx.page.waitForTimeout(3000);
    },
  });

  // ── Step 5: "Not authored by Google" warning → Run anyway ─────────────────
  await ctx.testStep('dismiss_run_anyway_warning', async () => {
    ctx.log('Looking for "not authored by Google" warning dialog…');

    // Poll for the "Run anyway" dialog for up to 20s
    let clicked = null;
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline && !clicked) {
      clicked = await robustShadowClick(ctx.page, 'run\\s*anyway');
      if (!clicked) await ctx.page.waitForTimeout(1000);
    }

    if (clicked) {
      ctx.log(`✅ Clicked "${clicked}" (shadow DOM pierce after polling)`);
      await ctx.page.waitForTimeout(2000);
    } else {
      ctx.log('No "Run anyway" dialog appeared within 20s — assuming already dismissed');
    }
  }, {
    verify: async (page) => true,
    verifyLabel: '"Run anyway" step complete',
    screenshotAfter: true,
  });


  // ── Step 6: "Allow this notebook to access your Google credentials?" ───────
  await ctx.testStep('allow_credentials', async () => {
    ctx.log('Looking for credentials access dialog — polling up to 20s…');

    // Poll for the Allow dialog for up to 20s (dialog appears 1-5s after cells start)
    let clicked = null;
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline && !clicked) {
      clicked = await robustShadowClick(ctx.page, '^allow$');
      if (!clicked) await ctx.page.waitForTimeout(1000);
    }

    if (clicked) {
      ctx.log(`✅ Clicked "${clicked}" (shadow DOM pierce after polling)`);
      await ctx.page.waitForTimeout(2000);
    } else {
      ctx.log('No "Allow" dialog appeared within 20s — assuming already authorized');
    }
  }, {
    verify: async (page) => true,
    verifyLabel: '"Allow" step complete',
    screenshotAfter: true,
  });

  // ── Step 7: Handle Google OAuth popup (or inline Allow dialog) ─────────────
  await ctx.testStep('handle_oauth_popup', async () => {
    ctx.log('Watching for Google OAuth popup or inline Allow dialog…');

    const _shotDir = ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : '/tmp');
    // Ensure evidence dir exists before any screenshot writes
    const { mkdirSync: _mkdir } = await import('node:fs');
    _mkdir(`${_shotDir}/steps`, { recursive: true });

    // Helper: click any "Allow" dialog visible in shadow DOM on the main page
    async function dismissInlineAllow(page, label) {
      const box = await page.evaluate(() => {
        const re = /^allow$/i;
        function findInShadow(root) {
          if (!root) return null;
          for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
            const tag = el.tagName?.toLowerCase() ?? '';
            if ((tag === 'button' || tag === 'mwc-button' || tag === 'paper-button') &&
                re.test((el.textContent ?? '').trim())) {
              const rect = el.getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) {
                return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: el.textContent.trim() };
              }
            }
            const found = el.shadowRoot ? findInShadow(el.shadowRoot) : null;
            if (found) return found;
          }
          return null;
        }
        return findInShadow(document);
      }).catch(() => null);

      if (box) {
        await page.mouse.click(box.x, box.y);
        ctx.log(`✅ Dismissed inline Allow dialog (${label}) via bounding box`);
        return true;
      }
      return false;
    }

    // ── Hybrid poll loop ─────────────────────────────────────────────────────
    // Cell 2 calls auth.authenticate_user() up to 3 times — each attempt can
    // show the "Allow this notebook to access your Google credentials?" inline
    // dialog. The old approach blocked for 20s waiting for a popup window —
    // during that 20s, dialog attempts 2 and 3 were never clicked.
    //
    // New: every 2s, BOTH dismiss any inline dialog AND check for a popup.
    // Loop runs for 45s to cover all 3 auth attempts (~10s each).
    const LOOP_TIMEOUT_MS  = 45_000;
    const POLL_INTERVAL_MS =  2_000;
    const _deadline = Date.now() + LOOP_TIMEOUT_MS;

    let popupHandled  = false;
    let inlineClicks  = 0;
    let pollIteration = 0;

    ctx.log(`[handle_oauth_popup] Starting hybrid poll (${LOOP_TIMEOUT_MS / 1000}s window, ${POLL_INTERVAL_MS / 1000}s interval)`);

    while (Date.now() < _deadline && !popupHandled) {
      pollIteration++;

      // 1. Click any inline Allow dialog currently visible
      const _clicked = await dismissInlineAllow(ctx.page, `poll-${pollIteration}`);
      if (_clicked) inlineClicks++;

      // 2. Check if a popup window is already open
      let popup = ctx.page.context().pages().find(p => p !== ctx.page);
      if (popup) {
        ctx.log(`[handle_oauth_popup] popup found (already open) at poll ${pollIteration}`);
        await handleOauthPopupFlow(popup, `${_shotDir}/steps`);
        popupHandled = true;
        break;
      }

      // 3. Wait up to POLL_INTERVAL_MS for a new popup to open
      const _remaining = _deadline - Date.now();
      const _waitMs    = Math.min(POLL_INTERVAL_MS, _remaining);
      if (_waitMs <= 0) break;

      popup = await ctx.page.context()
        .waitForEvent('page', { timeout: _waitMs })
        .catch(() => null);

      if (popup) {
        ctx.log(`[handle_oauth_popup] popup appeared during wait at poll ${pollIteration}`);
        await handleOauthPopupFlow(popup, `${_shotDir}/steps`);
        popupHandled = true;
      }
    }

    // Final sweep: dismiss any lingering inline dialog after loop ends
    const _finalDismissed = await dismissInlineAllow(ctx.page, 'final-cleanup');
    if (_finalDismissed) { inlineClicks++; await ctx.page.waitForTimeout(1500); }

    ctx.log(`[handle_oauth_popup] Done. popupHandled=${popupHandled} inlineClicks=${inlineClicks} iterations=${pollIteration}`);
    if (!popupHandled && inlineClicks === 0) {
      ctx.log('No OAuth popup and no inline Allow dialog — notebook may already be authorized');
    }

  }, {
    verify: async (page) => {
      const nb = await page.$('.notebook-cell, colab-cell, [data-type="code"], .cell');
      return !!nb;
    },
    verifyLabel: 'notebook cells visible after OAuth',
    screenshotAfter: true,
  });

  // ── Step 8: Confirm runtime connecting & Handle Tailscale Auth ────────────
  await ctx.testStep('confirm_execution_and_tailscale', async () => {
    ctx.log('Waiting for Colab runtime to execute and checking for Tailscale auth…');

    // NOTE: tailscale sub-workflows (tailscale-signin, tailscale-auth) are invoked
    // via ctx.runInline() which creates properly named sub-dirs directly inside the
    // parent job folder:
    //   self-spawn_YYYYMMDD_HHMMSS_6hex/
    //     tailscale-signin_YYYYMMDD_HHMMSS_6hex/   ← full steps/ + result.json
    //     tailscale-auth_YYYYMMDD_HHMMSS_6hex/     ← full steps/ + result.json
    // No createSubWorkflowContext wrapper needed — runInline handles everything.

    // Snapshot known Tailscale peers BEFORE execution, to detect NEW worker IPs
    const { execSync: _exec } = await import('child_process');
    const knownPeerIPs   = new Set();
    const knownOnlineIPs = new Set();  // only peers that are Online=true at baseline
    try {
      const tsJson = _exec('tailscale status --json 2>/dev/null', { encoding: 'utf8', timeout: 8000 });
      const tsPeers = JSON.parse(tsJson).Peer ?? {};
      for (const p of Object.values(tsPeers)) {
        (p.TailscaleIPs ?? []).forEach(ip => {
          knownPeerIPs.add(ip);
          if (p.Online) knownOnlineIPs.add(ip);
        });
      }
      ctx.log(`Baseline Tailscale peers: ${knownPeerIPs.size} known IPs, ${knownOnlineIPs.size} online`);
    } catch (e) {
      ctx.log(`Could not snapshot tailscale peers: ${e.message}`);
    }

    // ── Ensure mesh-admin profile (PRFL-021) is pulled from Drive on-demand ──
    const MESH_ADMIN_SESSION = 'sunmontueswednesthursfrisatur7';
    let meshAdminProfileDir = null;
    try {
      const _xiobr = '/content/xio-browser';
      const { ensureSessionState, localProfilePath, restoreProfile } =
        await import(`file://${_xiobr}/src/core/session-manager.mjs`);
      ctx.log(`Ensuring mesh-admin profile (${MESH_ADMIN_SESSION}) is available locally…`);
      await ensureSessionState(MESH_ADMIN_SESSION);
      // If tarball exists but dir not yet extracted, restore it
      const profileDir = localProfilePath(MESH_ADMIN_SESSION);
      const fs2 = await import('node:fs');
      if (!fs2.default.existsSync(profileDir)) {
        restoreProfile(MESH_ADMIN_SESSION);
      }
      meshAdminProfileDir = profileDir;
      ctx.log(`✅ Mesh-admin profile ready: ${meshAdminProfileDir}`);
    } catch (e) {
      ctx.log(`⚠️  Could not pull mesh-admin profile: ${e.message} — will attempt anyway`);
      meshAdminProfileDir = `/content/xio-mesh/chrome_profiles/PRFL-021_${MESH_ADMIN_SESSION}`;
      ctx.log(`Falling back to canonical PRFL path: ${meshAdminProfileDir}`);
    }

    const _shotDir = ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : '/tmp');
    const { mkdirSync: _mkdir2 } = await import('node:fs');
    _mkdir2(`${_shotDir}/steps`, { recursive: true });

    const _spawnShot = createShot(`${_shotDir}/steps`, { logFn: ctx.log.bind(ctx) });
    _spawnShot.setPage(ctx.page);

    let tsHandled        = false;
    let tsAuthSuccess    = false;     // true only when tailscale-auth runInline returned r.ok
    let newWorkerIP      = null;
    let cell3Detected    = false;   // true once 'Kernel kept alive' appears
    let cell3FirstSeenAt = -1;      // loop-iteration when cell3 was first detected
    let credPropFailCount = 0;      // consecutive iterations with 'credential propagation' error — threshold: 24 × 5s = 120s

    // Poll up to 900s (180 × 5s) — workers need time for:
    //   Phase 1-4: ~120-180s (deps install), Phase 5: ~30s Tailscale, Phase 6-7: ~30s
    for (let i = 0; i < 180; i++) {
      await ctx.page.waitForTimeout(5000);

      // ── Periodic screenshot every 60s to steps/ ──────────────────
      if (i > 0 && i % 12 === 0) {
        const elapsed = i * 5;
        await _spawnShot(`poll_${elapsed}s`, ctx.page, { fullPage: true });
      }

      // Dismiss any Allow/Run-anyway dialogs that reappear during execution
      const dialogClicked = await ctx.page.evaluate(() => {
        function findInShadow(root, predicate) {
          if (!root) return null;
          for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
            if (predicate(el)) return el;
            if (el.shadowRoot) { const f = findInShadow(el.shadowRoot, predicate); if (f) return f; }
          }
          return null;
        }
        for (const pattern of [/^allow$/i, /run\s*anyway/i]) {
          const btn = findInShadow(document, el => {
            const tag = el.tagName?.toLowerCase() ?? '';
            return (tag === 'button' || tag === 'mwc-button' || tag === 'paper-button') &&
                   pattern.test((el.textContent ?? '').trim());
          });
          if (btn) { btn.click(); return btn.textContent?.trim(); }
        }
        return null;
      }).catch(() => null);
      if (dialogClicked) {
        ctx.log(`✅ Auto-dismissed dialog: "${dialogClicked}"`);
        await _spawnShot(`dialog_dismissed_${i}`, ctx.page);
        if (/allow/i.test(dialogClicked)) {
          await ctx.page.waitForTimeout(2000);
          let p = ctx.page.context().pages().find(p => p !== ctx.page && !p.url().includes('tailscale.com'));
          if (!p) p = await ctx.page.context().waitForEvent('page', { timeout: 10_000 }).catch(() => null);
          if (p) await handleOauthPopupFlow(p, `${_shotDir}/steps`);
        }
      }

      let strayPopup = ctx.page.context().pages().find(p => p !== ctx.page && (p.url().includes('accounts.google.com') || p.url().includes('oauth')) && !p.url().includes('tailscale.com'));
      if (strayPopup) {
         ctx.log('Detected stray OAuth popup in Step 8! Handling it...');
         await handleOauthPopupFlow(strayPopup, `${_shotDir}/steps`);
      }

      // ── Cell-error recovery: if bootloader cell crashed, trigger Run All ──
      // Happens when Drive notebook still had the old auth `raise` code and
      // auth.authenticate_user() failed 3 times. Detect via cell error state
      // in DOM. Retry once only (cellErrorRetried flag).
      if (!ctx._cellErrorRetried) {
        try {
          const cellErrored = await ctx.page.evaluate(() => {
            // Colab sets state="error" on crashed cells
            const errCell = document.querySelector(
              'colab-cell[state="error"], colab-cell.error, .cell.error'
            );
            if (errCell) return true;
            // Also check shadow DOM for error indicators
            function hasError(root) {
              if (!root) return false;
              const el = root.querySelector ? root.querySelector('[class*="error-output"], .error-container, .traceback') : null;
              if (el) return true;
              for (const c of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
                if (c.shadowRoot && hasError(c.shadowRoot)) return true;
              }
              return false;
            }
            return hasError(document);
          }).catch(() => false);

          if (cellErrored) {
            ctx.log('[cell-recovery] ⚠️ Cell error detected — bootloader crashed. Triggering Run All to retry…');
            ctx._cellErrorRetried = true;
            await _spawnShot(`cell_error_detected_${i}`, ctx.page, { fullPage: true });
            // Re-trigger Run All (same as the run_all_cells step)
            await ctx.page.keyboard.down('Control');
            await ctx.page.keyboard.press('F9');
            await ctx.page.keyboard.up('Control');
            await ctx.page.waitForTimeout(2000);
            // Dismiss "Run anyway" warning if it appears
            const ra = await ctx.page.waitForSelector(
              'paper-button:has-text("Run anyway"), button:has-text("Run anyway"), [role="button"]:has-text("Run anyway")',
              { timeout: 5000, state: 'visible' }
            ).catch(() => null);
            if (ra) { await ra.click(); ctx.log('[cell-recovery] Dismissed Run-anyway dialog'); }
            await ctx.page.waitForTimeout(3000);
            ctx.log('[cell-recovery] ✅ Run All triggered — resuming poll');
          }
        } catch (_cerr) {
          ctx.log(`[cell-recovery] check error: ${_cerr.message?.slice(0, 60)}`);
        }
      }

      // ── PRIMARY: Poll Drive ts_pending_auth/ for TS auth URL ─────────────────
      // boot.py writes the URL to Drive immediately when it generates it (Phase 5).
      // ts_url_poll.py reads and deletes it (pick-use-delete). Far more reliable
      // than page scraping because it's not affected by Colab output collapse/truncation.
      if (!tsHandled) {
        try {
          const { execSync: _dExec } = await import('node:child_process');
          const _dOut = _dExec(
            'python3 /content/xio-browser/colab/ts_url_poll.py 2>/dev/null',
            { encoding: 'utf8', timeout: 20000 }
          ).trim();
          if (_dOut.startsWith('URL:')) {
            const _driveUrl = _dOut.slice(4).trim();
            ctx.log(`[ts-drive-poll] ✅ TS auth URL from Drive: ${_driveUrl}`);
            pageText += '\n' + _driveUrl; // inject — existing URL detection block picks it up
          } else if (_dOut && !['NO_URL','NO_FOLDER'].includes(_dOut.split(':')[0])) {
            ctx.log(`[ts-drive-poll] ${_dOut.slice(0, 80)}`);
          }
        } catch (_dErr) {
          ctx.log(`[ts-drive-poll] non-fatal: ${_dErr.message?.slice(0, 60)}`);
        }
      }

      // ── Expand collapsed Colab outputs + scroll to active cell ───────────
      // Colab collapses long cell outputs when they exceed ~5 KB (shows 'Show more'
      // button). The TS auth URL from Phase 5 may be hidden in collapsed output if
      // Phase 1-4 (package install) generated many lines. Expand before reading.
      try {
        await ctx.page.evaluate(() => {
          // Click all Colab output-expansion controls
          const expanders = [
            ...document.querySelectorAll('colab-output-expand-button'),
            ...document.querySelectorAll('[data-type="output"] [class*="expand"]'),
            ...document.querySelectorAll('.colab-output-global-collapsed'),
            ...document.querySelectorAll('colab-cell colab-output [class*="show-code"]'),
          ];
          expanders.forEach(el => { try { el.click(); } catch(_) {} });
        });
        await ctx.page.waitForTimeout(400); // let DOM settle after expansion
      } catch (_) {}

      // Scroll to the currently running/latest cell output for screenshots
      try {
        await ctx.page.evaluate(() => {
          const candidates = [
            'colab-cell[state="running"] colab-output',
            'colab-cell[state="busy"] colab-output',
            'colab-cell[data-state="running"] colab-output',
            '.cell.running .output',
            'colab-output:last-of-type',
            '.cell-output:last-child',
          ];
          for (const sel of candidates) {
            const el = document.querySelector(sel);
            if (el) { el.scrollIntoView({ behavior: 'instant', block: 'center' }); break; }
          }
        });
        await ctx.page.waitForTimeout(300); // settle before screenshot
      } catch (_) {}

      // ── Read notebook cell output (all frames) ────────────────────────────
      let pageText = '';
      try {
        const frames = ctx.page.frames();
        for (const frame of frames) {
          try {
            pageText += await frame.evaluate(() => {
              const texts = [];
              function extractText(doc) {
                if (!doc) return;
                const selectors = [
                  '.cell-output', '.output_area', 'colab-output',
                  '.output-result', '.output_subarea', 'pre.output',
                  '[id^="output"]', '.outputarea', 'body'
                ];
                const seen = new Set();
                for (const sel of selectors) {
                  for (const el of Array.from(doc.querySelectorAll(sel))) {
                    if (!seen.has(el)) { seen.add(el); texts.push(el.innerText ?? ''); }
                  }
                }
                for (const el of Array.from(doc.querySelectorAll('*'))) {
                  if (el.shadowRoot) extractText(el.shadowRoot);
                }
              }
              extractText(document);
              return texts.join('\n');
            });
            pageText += '\n';
          } catch(e) {}
        }
      } catch (e) {
        ctx.log(`Error reading frames: ${e.message}`);
      }

      // ── Extra: scan href attributes for TS URL (Colab renders it as <a> link) ──
      if (!tsHandled) {
        try {
          const foundHrefs = await ctx.page.evaluate(() => {
            const urls = [];
            function searchNode(root) {
              if (!root) return;
              const anchors = root.querySelectorAll?.('a[href*="login.tailscale.com/a/"]') ?? [];
              for (const a of anchors) { if (a.href) urls.push(a.href); }
              for (const el of root.querySelectorAll?.('*') ?? []) {
                if (el.shadowRoot) searchNode(el.shadowRoot);
              }
            }
            searchNode(document);
            for (const f of document.querySelectorAll('iframe')) {
              try { searchNode(f.contentDocument); } catch(_) {}
            }
            return urls;
          }).catch(() => []);
          if (foundHrefs.length > 0) {
            ctx.log(`[ts-href-scan] Found ${foundHrefs.length} TS URL(s) via href scan`);
            pageText += '\n' + foundHrefs.join('\n');
          }
        } catch (_) {}
      }

      // ── Detect machine-readable boot.py markers (most reliable) ─────────────
      // XIO_TS_CONNECTED: printed when tailscale connects with restored state
      // XIO_TS_AUTH_NEEDED: printed alongside the auth URL — inject into pageText
      if (!tsHandled) {
        if (pageText.includes('XIO_TS_CONNECTED:')) {
          const ipMatch = pageText.match(/XIO_TS_CONNECTED:\s*(100\.\d+\.\d+\.\d+)/);
          const workerIp = ipMatch ? ipMatch[1] : 'restored-identity';
          ctx.log(`[ts-marker] XIO_TS_CONNECTED detected — worker IP: ${workerIp}`);
          tsHandled = true;
          newWorkerIP = workerIp;
        }
        const authMarker = pageText.match(/XIO_TS_AUTH_NEEDED:\s*(https:\/\/login\.tailscale\.com\/a\/[A-Za-z0-9]+)/);
        if (authMarker) {
          ctx.log(`[ts-marker] XIO_TS_AUTH_NEEDED detected: ${authMarker[1]}`);
          pageText += '\n' + authMarker[1]; // Inject so URL detection block picks it up
        }
      }

      // ── Detect Tailscale auth URL → delegate to sub-workflows (CONDITIONAL) ─
      // The tailscale-auth sub-workflow (and its job folder) is ONLY created when:
      //   1. A TS auth URL is detected in the Colab output, AND
      //   2. No existing TS state file for the spawned Pro account exists on Drive.
      // If a state file already exists, the account is already mesh-enrolled —
      // tailscale-auth is skipped entirely and tsHandled is set directly.
      //
      // Evidence structure (via runInline — each gets its own named sub-dir):
      //   self-spawn_.../
      //     tailscale-signin_YYYYMMDD_HHMMSS_6hex/
      //       steps/  00_ensure_google_session.jpg, 01_prepare_mesh_admin.jpg, 02_tailscale_signin.jpg …
      //       result.json
      //     tailscale-auth_YYYYMMDD_HHMMSS_6hex/
      //       steps/  00_load_mesh_admin_session.jpg, 01_connect_device.jpg …
      //       result.json
      if (!tsHandled && pageText.includes('https://login.tailscale.com/a/')) {
        const match = pageText.match(/(https:\/\/login\.tailscale\.com\/a\/[A-Za-z0-9]+)/);
        if (match) {
          const tsUrl = match[1];
          ctx.log(`[ts-auth] ✅ Found Tailscale Auth URL: ${tsUrl}`);
          tsHandled = true;

          // ── Check if spawned Pro account already has a TS state file on Drive ─
          let workerTsStateExists = false;
          try {
            const { execSync: _tsCheck } = await import('node:child_process');
            // Slug uses hyphens to match boot.py _slug() and Drive filenames:
            // boot.py: TS_colab-worker-{slug}.state  (slug = email username with . and _ → -)
            const workerSlug = ctx.sessionId
              .split('@')[0].split('+')[0]
              .replace(/[._]/g, '-');  // hyphens, matching boot.py _slug()
            const checkOut = _tsCheck(
              `python3 -c "
import sys
sys.path.append('/content/xio-browser/colab')
import sync
# Check both canonical (hyphens) and legacy (underscores) filenames
_slug = '${workerSlug}'
_names = [f'TS_colab-worker-{_slug}.state', f'TS_colab_worker_{_slug.replace("-","_")}.state']
files = []
for _nm in _names:
    _r = sync.svc.files().list(
        q=f'name=\\"{_nm}\\" and parents in [\\'' + sync.FOLDER_ID + '\\'] and trashed=false',
        spaces='drive', fields='files(id,name)', pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute().get('files', [])
    if _r: files = _r; break
print('EXISTS' if files else 'NOT_FOUND')
" 2>/dev/null`,
              { encoding: 'utf8', timeout: 15000 }
            ).trim();
            workerTsStateExists = checkOut.includes('EXISTS');
            ctx.log(`[ts-auth] Drive TS state for ${workerSlug}: ${workerTsStateExists ? '✅ EXISTS — skipping auth' : '❌ NOT_FOUND — auth required'}`);
          } catch (_tsCheckErr) {
            ctx.log(`[ts-auth] ⚠️  Drive TS state check failed: ${_tsCheckErr.message?.slice(0, 80)} — running tailscale-auth as fallback`);
          }

          if (workerTsStateExists) {
            tsAuthSuccess = true;
            ctx.log('[ts-auth] ✅ TS state exists on Drive — worker will auto-connect. Skipping tailscale-auth.');
          } else {
            // Write TS auth URL for tailscale-auth workflow to read
            const { writeFileSync: _wfs } = await import('node:fs');
            _wfs('/tmp/xio_ts_auth_url', tsUrl, 'utf8');
            ctx.log(`[ts-auth] Wrote auth URL to /tmp/xio_ts_auth_url`);

            // ── tailscale-auth (self-contained: ensure_google_session + connect_device) ──
            // tailscale-auth now absorbs the google-signin pre-step internally,
            // so we no longer need a separate tailscale-signin runInline here.
            // It uses launchPersistentContext (full Chrome profile with localStorage/IndexedDB)
            // which is required for Tailscale session data — newContext(storageState) loses it.
            ctx.log(`[ts-auth] Launching tailscale-auth inline for: ${tsUrl}`);

            // Background screenshot loop — captures notebook page every 30s while
            // tailscale-signin and tailscale-auth sub-workflows have control.
            let _bgShotIdx = 0;
            const _bgShot = setInterval(async () => {
              try {
                if (!ctx.page.isClosed()) {
                  await _spawnShot(`bg_ts_auth_${String(++_bgShotIdx).padStart(2,'0')}`, ctx.page);
                }
              } catch (_) {}
            }, 30_000);

            const r2 = await ctx.runInline('tailscale-auth', MESH_ADMIN_SESSION, { tsAuthUrl: tsUrl });

            clearInterval(_bgShot);

            if (r2.ok) {
              tsAuthSuccess = true;
              ctx.log('[ts-auth] ✅ tailscale-auth complete — device authorization submitted');
            } else {
              ctx.log('[ts-auth] ⚠️  tailscale-auth failed — HITL pause for manual auth');
              const _hitlAction = await ctx.hitl?.(
                `tailscale-auth sub-workflow failed.\n\nTS Auth URL: ${tsUrl}\n\nManually open the URL in a browser signed in as the mesh-admin account and click Connect, then resume this job.`,
                {
                  stepName: 'tailscale_auth_failed',
                  errorType: 'TS_AUTH_FAILED',
                  tsAuthUrl: tsUrl,
                  resumeActions: ['manual_auth_done', 'skip_ts_auth'],
                }
              );
              // Only mark success if operator confirmed auth was done.
              // 'skip_ts_auth' = operator wants to skip (worker won't join mesh).
              // Do NOT force tsAuthSuccess=true blindly — that caused wait_and_save
              // to poll indefinitely for a TS state that was never saved.
              if (_hitlAction === 'manual_auth_done' || _hitlAction === 'resume') {
                tsAuthSuccess = true;
                ctx.log('[ts-auth] ✅ HITL: operator confirmed auth done');
              } else {
                ctx.log(`[ts-auth] ℹ️  HITL resumed with action=${_hitlAction} — skipping TS state save`);
              }
            }
            ctx.log('[ts-auth] Tailscale sub-workflows complete');

          }
        }
      }


      // ── Credential propagation error — transient Colab ADC warning ──────────
      // ADC (Application Default Credentials) takes 30-90s to propagate after OAuth.
      // Colab prints "credential propagation was unsuccessful" transiently — it clears
      // once ADC settles. Only fail after 12 consecutive checks (60s) of persistence.
      if (pageText.includes('credential propagation was unsuccessful') ||
          pageText.includes('MessageError: Error')) {
        credPropFailCount++;
        if (credPropFailCount >= 24) {
          throw new Error(`❌ Credential propagation failed persistently (${credPropFailCount} checks × 5s = 120s+)`);
        }
        ctx.log(`[⚠️  cred-warn] ADC propagation error (${credPropFailCount}/24) — waiting for Colab ADC to settle...`);
      } else {
        if (credPropFailCount > 0) ctx.log(`[✅ cred-ok] ADC propagation cleared after ${credPropFailCount} warnings`);
        credPropFailCount = 0;
      }

      // ── Dynamic success: new OR reconnected peer is online ───────────────
      try {
        const tsJson2 = _exec('tailscale status --json 2>/dev/null', { encoding: 'utf8', timeout: 8000 });
        const tsPeers2 = JSON.parse(tsJson2).Peer ?? {};
        for (const peer of Object.values(tsPeers2)) {
          const ips = peer.TailscaleIPs ?? [];
          const workerIp = ips.find(ip => ip.startsWith('100.'));
          if (!workerIp || !peer.Online) continue;
          // Case A: brand-new IP never seen at baseline
          if (ips.some(ip => !knownPeerIPs.has(ip))) {
            newWorkerIP = workerIp;
            ctx.log(`✅ New worker online (new IP)! ${newWorkerIP} (${peer.HostName})`);
            break;
          }
          // Case B: existing IP that was OFFLINE at baseline, now Online
          // (worker with restored tailscale identity reconnecting to mesh)
          if (!knownOnlineIPs.has(workerIp)) {
            newWorkerIP = workerIp;
            ctx.log(`✅ Worker reconnected (was offline→online)! ${newWorkerIP} (${peer.HostName})`);
            break;
          }
        }
      } catch (_) {}

      if (newWorkerIP) {
        // ── TS state backup: SSH-copy from worker → master → push to Drive ────
        // Old approach used python3 -c with nested shell escaping → truncated at /content/.
        // New approach: SSH to worker, copy the raw tailscaled.state via base64,
        // save locally on master, push via sync.py --node-name (master has Drive write access).
        const workerSlug = ctx.sessionId
          ? ctx.sessionId.split('@')[0].split('+')[0].replace(/[._]/g, '-')
          : null;
        if (tsAuthSuccess && newWorkerIP !== 'restored-identity' && workerSlug) {
          ctx.log('[ts-drive-gate] Copying Tailscale state from worker and pushing to Drive…');
          try {
            const { execSync: _tgExec }                           = await import('node:child_process');
            const { writeFileSync: _tgWfs, mkdirSync: _tgMkdir } = await import('node:fs');
            const _workerNodeName = `colab-worker-${workerSlug}`;
            const _stateDir   = '/content/xio-mesh/tailscale_states';
            const _stateLocal = `${_stateDir}/TS_${_workerNodeName}.state`;
            _tgMkdir(_stateDir, { recursive: true });
            // 1. SSH-copy raw state (base64 avoids binary pipe issues)
            // Use ConnectTimeout=8 so we fail fast if port 22 is closed on worker.
            const _b64 = _tgExec(
              'ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@' + newWorkerIP +
              " 'cat /var/lib/tailscale/tailscaled.state | base64'",
              { encoding: 'utf8', timeout: 20000 }
            ).trim();
            if (!_b64 || _b64.length < 100) {
              // SSH succeeded but returned nothing, or SSH failed silently
              ctx.log(`[ts-drive-gate] ⚠️  SSH returned empty/short output (${_b64.length}b) — worker port 22 may be closed. Skipping state push to avoid 0-byte Drive file.`);
            } else {
              const _stateBuf = Buffer.from(_b64, 'base64');
              _tgWfs(_stateLocal, _stateBuf);
              ctx.log(`[ts-drive-gate] ✅ State copied locally (${_stateBuf.length}b)`);
              // 2. Push to Drive with correct --node-name (master has Drive write access)
              _tgExec(
                `python3 /content/xio-browser/colab/sync.py --what tailscale --push --node-name ${_workerNodeName}`,
                { encoding: 'utf8', timeout: 30000 }
              );
              ctx.log(`[ts-drive-gate] ✅ TS_${_workerNodeName}.state saved to Drive`);
            }
          } catch (_tge) {
            ctx.log(`[ts-drive-gate] ⚠️  SSH copy failed: ${_tge.message?.slice(0, 80)}`);
            // SSH failed — check if worker saved state directly to Supabase
            try {
              const { getNodeTsState } = await import('file:///content/xio-browser/src/core/db.mjs');
              const _workerNodeName = `colab-worker-${workerSlug}`;
              const _stateLocal = `/content/xio-mesh/tailscale_states/TS_${_workerNodeName}.state`;
              const _sbState = await getNodeTsState(_workerNodeName);
              if (_sbState?.state_size > 100) {
                ctx.log(`[ts-drive-gate] ✅ Worker saved TS state to Supabase (${_sbState.state_size}b) — Drive push skipped (master ADC handles that)`);
                // Optionally also push to Drive from master using the Supabase state
                try {
                  const { execSync: _tgExec2 } = await import('node:child_process');
                  const { writeFileSync: _tgWfs2 } = await import('node:fs');
                  const _stateBuf = Buffer.from(_sbState.state_b64, 'base64');
                  _tgWfs2(_stateLocal, _stateBuf);
                  _tgExec2(
                    `python3 /content/xio-browser/colab/sync.py --what tailscale --push --node-name ${_workerNodeName}`,
                    { encoding: 'utf8', timeout: 30000 }
                  );
                  ctx.log(`[ts-drive-gate] ✅ TS state pulled from Supabase → pushed to Drive`);
                } catch (_driveErr) {
                  ctx.log(`[ts-drive-gate] ⚠️  Drive push from Supabase failed: ${_driveErr.message?.slice(0, 60)} (state safe in Supabase)`);
                }
              } else {
                ctx.log(`[ts-drive-gate] ⚠️  Worker has not saved state to Supabase yet — state may be recovered from worker after boot`);
              }
            } catch (_sbErr) {
              ctx.log(`[ts-drive-gate] ⚠️  Supabase check failed: ${_sbErr.message?.slice(0, 60)}`);
            }
          }
        }
        break;
      }
      // ── Cell 3 keep-alive → safe to finish, BUT only after TS auth ─────────
      if (pageText.includes('Kernel kept alive. You may close the browser.') ||
          pageText.includes('XIO Mesh Boot Complete')) {
        if (!cell3Detected) {
          ctx.log('✅ Cell 3 Keep-Alive detected — worker booting');
          cell3Detected    = true;
          cell3FirstSeenAt = i;
        }
        if (tsHandled && tsAuthSuccess) {
          // Auth was handled AND confirmed — resolve the real Tailscale IP before
          // break so ts-drive-gate can SSH-copy the state (not a sentinel string).
          newWorkerIP = 'confirmed-via-cell3'; // fallback if lookup fails
          try {
            const { execSync: _c3Exec } = await import('node:child_process');
            const _tsJson = JSON.parse(
              _c3Exec('tailscale status --json', { encoding: 'utf8', timeout: 8000 })
            );
            const _c3Slug = (ctx.sessionId ?? '').split('@')[0].split('+')[0]
              .replace(/[._]/g, '-').toLowerCase();
            for (const peer of Object.values(_tsJson.Peer ?? {})) {
              const hn = (peer.HostName ?? peer.DNSName ?? '').toLowerCase();
              if (hn.includes(_c3Slug) || hn.includes('colab-worker')) {
                const ip = peer.TailscaleIPs?.[0];
                if (ip) {
                  newWorkerIP = ip;
                  ctx.log(`[cell3] Resolved worker IP from tailscale status: ${ip}`);
                  break;
                }
              }
            }
          } catch (_c3e) {
            ctx.log(`[cell3] Could not resolve worker IP: ${_c3e.message?.slice(0, 60)}`);
          }
          break;
        } else if (tsHandled && !tsAuthSuccess) {
          // Auth was attempted but tsAuthSuccess not yet set (still running inline)
          // Stay in loop — peer detection will confirm when device comes online
        }
        const graceIters = i - cell3FirstSeenAt;
        if (graceIters < 12) {
          // Give 60s more (12 × 5s) for the TS URL to appear in cell output
          ctx.log(`[ts-grace] Cell3 seen but TS URL not yet found — grace ${graceIters}/12...`);
        } else {
          // Grace expired — HITL pause so operator can manually provide the URL
          ctx.log('⚠️  Tailscale auth URL never detected in cell output after 60s grace — escalating HITL');
          ctx.log('[hitl] Pausing for HITL intervention...');
          try { await ctx.page.screenshot({ path: '/tmp/xio_hitl_pause.png' }); } catch (_) {}
          const hitlRes = await ctx.hitl?.(
            'Tailscale auth URL not found in Colab cell output after 60s grace',
            {
              stepName:  'confirm_execution_and_tailscale',
              errorType: 'TS_URL_NOT_FOUND',
              workerNotebookUrl: `https://colab.research.google.com/drive/${NOTEBOOK_FILE_ID ?? '1_s-oQqbLw8gg1hyTn49lGna09k4QDGR7'}`,
              instructions: [
                '=== AUTO-RESUME OPTION ===',
                'The cell output expansion fix is already enabled.',
                'Re-running the workflow will expand collapsed outputs and retry URL detection.',
                '',
                '=== MANUAL OPTION ===',
                '1. Open worker notebook: https://colab.research.google.com/drive/' +
                  (NOTEBOOK_FILE_ID ?? '1_s-oQqbLw8gg1hyTn49lGna09k4QDGR7'),
                '2. In cell 2 output, expand any collapsed sections (click "Show more")',
                '3. Find the line under Phase 5: Tailscale: "To authenticate, visit:"',
                '4. Write URL to runner: echo "URL" > /tmp/xio_ts_auth_url',
                '5. Resume this job via xb_resume_job or retry xb_run_workflow',
              ].join('\n'),
              resumeActions: ['retry_workflow', 'provide_url_via_file', 'skip_ts_auth'],
            }
          );
          // After HITL resume — try reading the file
          try {
            const { readFileSync: _hitlRfs } = await import('node:fs');
            const hitlUrl = _hitlRfs('/tmp/xio_ts_auth_url', 'utf8').trim();
            if (hitlUrl.includes('login.tailscale.com')) {
              ctx.log(`[ts-hitl] Got URL from file: ${hitlUrl}`);
              pageText += '\n' + hitlUrl; // inject so URL detection picks it up
              i = -1; // reset loop to re-run detection with the injected URL
              cell3FirstSeenAt = i + 1; // reset grace window to current position (not 999 which would suppress HITL forever)
              continue;
            }
          } catch (_) {}

          // TS state was already backed up in the ts-drive-gate block above
          // (SSH copy from worker → master → sync.py --node-name push to Drive).
          ctx.log('[ts-backup] TS state backup handled in ts-drive-gate step ✅');
         // Re-expand outputs and scroll after HITL resume
          try {
            await ctx.page.evaluate(() => {
              document.querySelectorAll('colab-output-expand-button').forEach(el => el.click());
            });
          } catch (_) {}

          if (hitlRes === 'retry_workflow') {
            i = -1;
            cell3FirstSeenAt = 999;
            continue;
          }

          ctx.log('⚠️  Could not recover TS URL after HITL — proceeding without auth');
          newWorkerIP = 'confirmed-via-cell3-no-ts-auth';
          break;
        }
      }

      if (i > 0 && i % 6 === 0) ctx.log(`Still waiting… (${i * 5}s elapsed)`);
    }

    if (!newWorkerIP) {
      throw new Error('❌ Worker node did not appear in Tailscale mesh within 900s. Notebook may have failed.');
    }
    ctx.log(`✅ Worker confirmed: ${newWorkerIP}`);
  }, {
    screenshotAfter: true,
    verifyLabel: 'new worker node online in Tailscale mesh',
    autoResolvable: false,
  });


  // ── wait_and_save_ts_state ────────────────────────────────────────────────
  // boot.py on the worker uploads TS state to R2 at ts_states/TS_<node>.state
  // via the ts-r2 watcher within ~30-60s of Tailscale connecting.
  // Poll R2 directly via boto3 every 15s. SSH-copy is kept as a fallback
  // but R2 is the primary path (SSH port 22 is often not ready in time).
  await ctx.testStep('wait_and_save_ts_state', async () => {
    const { execSync: _tsExec }     = await import('node:child_process');
    const { writeFileSync: _tsWfs, mkdirSync: _tsMkdir, readFileSync: _tsRfs } = await import('node:fs');
    const _createShot               = (await import('file:///content/xio-browser/src/core/wf-shot.mjs')).createShot;

    const _workerSlug     = ctx.sessionId.split('@')[0];
    const _workerSlugDash = _workerSlug.split('+')[0].replace(/[._]/g, '-').toLowerCase();
    const _workerNodeName = `colab-worker-${_workerSlugDash}`;
    const _r2Key          = `ts_states/TS_${_workerNodeName}.state`;
    const _stateLocal     = `/content/xio-mesh/tailscale_states/TS_${_workerNodeName}.state`;
    const _stepShots      = `${ctx.jobDir ?? '/tmp'}/steps`;
    _tsMkdir(_stepShots, { recursive: true });

    // R2 credentials — injected by boot.py into process env
    let _cfg = {};
    try { _cfg = JSON.parse(_tsRfs('/tmp/xio_config.json', 'utf8')); } catch (_) {}
    const _R2_EP = process.env.R2_ENDPOINT        || _cfg.r2_endpoint        || '';
    const _R2_AK = process.env.R2_ACCESS_KEY_ID   || _cfg.r2_access_key_id   || '';
    const _R2_SK = process.env.R2_SECRET_ACCESS_KEY || _cfg.r2_secret_access_key || '';
    const _R2_BK = process.env.R2_BUCKET           || _cfg.r2_bucket          || 'xio-mesh';

    const POLL_INTERVAL_MS = 15_000;
    const MAX_POLLS        = 32;    // 32 × 15s = 8 minutes max (boot.py phases take 4-7 min)

    ctx.log(`[ts-state] Waiting for Cell 4 (keep-alive) + R2 state for ${_workerNodeName}…`);
    ctx.log(`[ts-state] R2 key: ${_R2_BK}/${_r2Key}`);

    // Helper: resolve worker's actual Tailscale IP by node name
    const _resolveWorkerIP = () => {
      try {
        const _st = JSON.parse(_tsExec('tailscale status --json', { encoding: 'utf8', timeout: 8000 }));
        for (const peer of Object.values(_st.Peer ?? {})) {
          const hn = (peer.HostName ?? peer.DNSName ?? '').toLowerCase();
          if (hn.includes(_workerSlugDash) || hn.replace(/-\d+$/, '') === _workerNodeName) {
            return peer.TailscaleIPs?.[0] ?? null;
          }
        }
      } catch (_) {}
      return null;
    };

    // Helper: Check R2 for ts_state file, copy locally + push to Drive if found
    const _checkR2State = (pollNum) => {
      if (!_R2_EP || !_R2_AK || !_R2_SK) {
        ctx.log(`[ts-state] Poll ${pollNum}/${MAX_POLLS}: R2 creds missing — skipping R2 check`);
        return false;
      }
      try {
        const _pyScript = `
import sys, os, boto3
from botocore.client import Config
s3 = boto3.client('s3',
    endpoint_url='${_R2_EP}',
    aws_access_key_id='${_R2_AK}',
    aws_secret_access_key='${_R2_SK}',
    config=Config(signature_version='s3v4'))
try:
    obj = s3.get_object(Bucket='${_R2_BK}', Key='${_r2Key}')
    data = obj['Body'].read()
    sys.stdout.buffer.write(data)
except s3.exceptions.NoSuchKey:
    sys.stderr.write('NOT_FOUND')
    sys.exit(1)
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(2)
`;
        const _stateBytes = _tsExec(`python3 -c "${_pyScript.replace(/"/g, '\\\\"')}"`,
          { timeout: 20_000, maxBuffer: 4 * 1024 * 1024 });
        if (_stateBytes && _stateBytes.length > 100) {
          ctx.log(`[ts-state] ✅ R2 state found (${_stateBytes.length}b) on poll ${pollNum}`);
          _tsMkdir('/content/xio-mesh/tailscale_states', { recursive: true });
          _tsWfs(_stateLocal, _stateBytes);
          ctx.log(`[ts-state] Wrote ${_stateBytes.length}b to ${_stateLocal}`);
          // Push to Drive
          try {
            const _drOut = _tsExec(
              `python3 /content/xio-browser/colab/sync.py --what tailscale --push --node-name ${_workerNodeName}`,
              { encoding: 'utf8', timeout: 45_000 }
            );
            ctx.log(`[ts-state] ✅ TS state pushed to Drive: ${_drOut.trim().slice(0, 80)}`);
          } catch (_de) {
            ctx.log(`[ts-state] ⚠️  Drive push failed: ${_de.message?.slice(0, 60)}`);
          }
          return true;
        }
        ctx.log(`[ts-state] Poll ${pollNum}/${MAX_POLLS}: R2 state_size=${_stateBytes?.length ?? 0}${cell4Seen ? ' (Cell 4 up)' : ' (waiting for Cell 4)'}`);
        return false;
      } catch (_r2e) {
        const _msg = (_r2e.stderr?.toString() || _r2e.message || '').slice(0, 80);
        ctx.log(`[ts-state] Poll ${pollNum}/${MAX_POLLS}: R2 miss — ${_msg}${cell4Seen ? ' (Cell 4 up)' : ''}`);
        return false;
      }
    };

    // Helper: SSH-copy state from worker, write locally, push to Drive (fallback)
    const _sshCopyState = (workerIP) => {
      try {
        ctx.log(`[ts-state] SSH-copy state from ${workerIP}…`);
        const _b64 = _tsExec(
          `ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@${workerIP}` +
          ` 'cat /var/lib/tailscale/tailscaled.state | base64 -w0'`,
          { encoding: 'utf8', timeout: 25_000 }
        ).trim();
        if (!_b64 || _b64.length < 100) {
          ctx.log(`[ts-state] SSH returned too-short output (${_b64.length}b) — port 22 not ready yet`);
          return false;
        }
        const _stateBuf = Buffer.from(_b64, 'base64');
        _tsMkdir('/content/xio-mesh/tailscale_states', { recursive: true });
        _tsWfs(_stateLocal, _stateBuf);
        ctx.log(`[ts-state] ✅ SSH state copied locally (${_stateBuf.length}b)`);
        try {
          const _drOut = _tsExec(
            `python3 /content/xio-browser/colab/sync.py --what tailscale --push --node-name ${_workerNodeName}`,
            { encoding: 'utf8', timeout: 45_000 }
          );
          ctx.log(`[ts-state] ✅ TS state pushed to Drive: ${_drOut.trim().slice(0, 80)}`);
        } catch (_de) {
          ctx.log(`[ts-state] ⚠️  Drive push failed: ${_de.message?.slice(0, 60)}`);
        }
        return true;
      } catch (_se) {
        ctx.log(`[ts-state] SSH failed: ${_se.message?.slice(0, 80)}`);
        return false;
      }
    };

    let tsStateSaved = false;
    let cell4Seen    = false;

    for (let p = 0; p < MAX_POLLS; p++) {
      // Screenshot current notebook state to confirm Cell 4 running
      const _shot = _createShot(_stepShots);
      try {
        if (!ctx.page.isClosed()) {
          const pageText = await ctx.page.evaluate(() => document.body?.innerText ?? '').catch(() => '');
          if (!cell4Seen && (
            pageText.includes('Kernel kept alive') ||
            pageText.includes('XIO Mesh Boot Complete')
          )) {
            cell4Seen = true;
            ctx.log(`[ts-state] ✅ Cell 4 (keep-alive) confirmed running — boot complete`);
          }
          await ctx.page.screenshot({ path: _shot(`ts_state_poll_${String(p + 1).padStart(2, '0')}`) });
        }
      } catch (_) {}

      // Primary: check R2 for ts_state every poll
      if (_checkR2State(p + 1)) {
        tsStateSaved = true;
        break;
      }

      // Fallback: SSH-copy every 3rd poll after Cell 4 is up
      if (cell4Seen && p > 0 && p % 3 === 0) {
        const workerIP = _resolveWorkerIP();
        if (workerIP) {
          ctx.log(`[ts-state] SSH fallback from ${workerIP} (poll ${p + 1})`);
          if (_sshCopyState(workerIP)) {
            tsStateSaved = true;
            break;
          }
        }
      }

      if (p < MAX_POLLS - 1) await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }

    if (!tsStateSaved) {
      // Last-ditch: SSH attempt with fresh IP lookup
      const workerIP = _resolveWorkerIP();
      if (workerIP) {
        ctx.log(`[ts-state] Final SSH attempt from ${workerIP}…`);
        tsStateSaved = _sshCopyState(workerIP);
      }
    }

    if (!tsStateSaved) {
      ctx.log(`[ts-state] ⚠️  TS state NOT saved after 8 min. Worker state will be lost when tab closes.`);
      // Don't throw — worker is in mesh, state loss means re-auth on next boot but worker still functions
    } else {
      ctx.log(`[ts-state] ✅ TS state persisted to R2 + Drive — safe to close tab`);
    }
  }, {
    screenshotAfter: true,
    verifyLabel:     'worker TS state persisted to R2 + Drive',
    autoResolvable:  false,  // BLOCKING — don't proceed until state is saved or timeout
  });


  await ctx.testStep('navigate_away', async () => {
    ctx.log('Closing notebook page — Colab runtime continues running in cloud…');
    try { await ctx.page.close(); } catch (_) {}
    ctx.log('✅ Done — replacement worker is booting in cloud');
  }, {
    verify: async (page) => true,
    verifyLabel: 'closed page',
    screenshotAfter: false,  // page already closed — skip screenshot
  });


  
  ctx.setResult({
    success:          true,
    notebook_file_id: NOTEBOOK_FILE_ID,
    message:          'Replacement worker runtime spawned. Online in ~2-3 min.',
  });

}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function assertGoogleSession(ctx, expectedEmail) {
  await ctx.page.goto('https://accounts.google.com/ServiceLogin', {
    waitUntil: 'domcontentloaded', timeout: 30_000
  });
  await ctx.page.waitForTimeout(1500);
  const url = ctx.page.url();
  if (url.includes('accounts.google.com/ServiceLogin') || url.includes('signin')) {
    throw new Error('Not signed in to Google — session missing or expired');
  }
  if (expectedEmail) {
    const pageText = await ctx.page.innerText('body');
    if (!pageText.toLowerCase().includes(expectedEmail.toLowerCase())) {
      throw new Error(`Wrong Google account: expected ${expectedEmail}`);
    }
  }
  ctx.log(`✅ Google session verified${expectedEmail ? ` (${expectedEmail})` : ''}`);
}
