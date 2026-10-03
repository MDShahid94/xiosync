/**
 * google-signin.mjs — XIOSYNC Native
 * ─────────────────────────────────────────────────────────────────────────────
 * Google sign-in using UC stealth sidecar + patchright CDP.
 * Zero dependency on XIOBR / xio-browser. Runs inside XIOSYNC ScriptRunner.
 *
 * Architecture:
 *   Phase 0: Check if already logged in (skip if force=true)
 *   Phase 1: UC stealth sidecar (Python subprocess) → saves cookies to JSON
 *   Phase 2: Load cookies from JSON into live patchright browser context
 *   Phase 3: Navigate to myaccount.google.com to verify
 *
 * Params (all resolved from context JSON):
 *   email           Google account email (required)
 *   password        Account password (required)
 *   totp_secret     Base32 TOTP secret for 2FA (required)
 *   cdp_ws_url      CDP WebSocket URL of the target browser (required)
 *   session_id      XIOSYNC session UUID (optional, for push-profile)
 *   xiorun_url      xiorun-agent base URL e.g. http://100.97.124.3:9300
 *   force           Skip Phase 0 session check (default: false)
 *
 * Subprocess contract (stdout, last JSON line wins):
 *   { "status": "success", "result": {...} }
 *   { "status": "error",   "error": "..." }
 *
 * Note: Later this will be converted to a DAG-based xioflow template.
 */

export const meta = {
  name:        'google-signin',
  description: 'Google sign-in via UC stealth sidecar + patchright CDP. XIOSYNC-native, no XIOBR deps.',
  params: {
    email:                'Google account email (required)',
    password:             'Account password (required)',
    totp_secret:          'Base32 TOTP secret for 2FA (required)',
    cdp_ws_url:           'CDP WebSocket URL of the target browser session (required)',
    session_id:           'XIOSYNC session UUID (optional)',
    xiorun_url:           'xiorun-agent base URL (optional, e.g. http://100.97.124.3:9300)',
    proxy_url:            'PPPoE exit node SOCKS5 proxy URL (e.g. socks5://100.x.x.x:10001) — enforced on UC login',
    exit_node_public_ip:  'PPPoE slot public IP for geo-timezone resolution (optional)',
    force:                'Skip Phase 0 session check (default: false)',
  },
};


// ── Node.js stdlib only — no XIOBR imports ───────────────────────────────────
import { existsSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { tmpdir }                from 'node:os';
import { join }                  from 'node:path';
import { spawn }                 from 'node:child_process';
import { randomUUID }            from 'node:crypto';
import { fileURLToPath }         from 'node:url';

// ── Helpers ───────────────────────────────────────────────────────────────────
const randInt    = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;
const sleep      = ms => new Promise(r => setTimeout(r, ms));
const humanSleep = (a = 500, b = 1500) => sleep(randInt(a, b));

// ── [DEPRECATED] UC stealth sidecar removed ──────────────────────────────────
// The embedded Python UC script (_UC_SCRIPT) and runUCSidecar() were removed
// in the v2 architecture. Login now runs entirely on xiorun_agent.py via
// POST /run-uc-login. See: colab/xiorun_agent.py :: _run_uc_login_sync()

// ── Profile push (via xiorun-agent) ──────────────────────────────────────────
async function pushProfileToStorage({ session_id, identity_id, profile_dir, xiorun_url, log }) {
  // Phase 3.5 persist-session already handles Drive persistence for new profiles.
  // This function handles the legacy /push-profile path as a belt-and-suspenders
  // fallback — only fires when both identity_id and profile_dir are known.
  if (!xiorun_url) {
    log('[google-signin] Profile push skipped (no xiorun_url in context)');
    return;
  }
  if (!identity_id || !profile_dir) {
    log(`[google-signin] Profile push skipped (missing identity_id=${identity_id} or profile_dir=${profile_dir})`);
    return;
  }
  try {
    // Build the drive object key: profiles/PRFL-{serial}.tar.gz
    // The agent resolves the PRFL serial from identity_id internally;
    // we pass drive_object_key as a hint — agent will ignore if it already
    // knows a canonical key from the session.
    const driveKey = `profiles/PRFL-${(identity_id || '').slice(0, 8)}.tar.gz`;
    const body = JSON.stringify({
      identity_id,
      local_dir:        profile_dir,
      drive_object_key: driveKey,
    });
    const fullUrl = new URL(`${xiorun_url}/push-profile`);
    const isHttps = fullUrl.protocol === 'https:';
    const { request } = await import(isHttps ? 'node:https' : 'node:http');
    await new Promise((resolve, reject) => {
      const req = request({
        hostname: fullUrl.hostname,
        port:     fullUrl.port || (isHttps ? 443 : 80),
        path:     fullUrl.pathname,
        method:   'POST',
        headers:  { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      }, res => {
        let data = '';
        res.on('data', c => { data += c; });
        res.on('end', () => {
          log(`[google-signin] Profile push response (${res.statusCode}): ${data.slice(0, 100)}`);
          resolve();
        });
      });
      req.on('error', reject);
      req.write(body);
      req.end();
    });
  } catch (e) {
    log(`[google-signin] Profile push failed (non-fatal): ${e.message}`);
  }
}

// ── Proxy resolution: PPPoE slot → SSH tunnel fallback ───────────────────────
/**
 * Resolve the exit-node proxy URL for this login session.
 *
 * Priority:
 *   1. Explicit proxy_url param (caller already acquired a slot)
 *   2. Acquire an idle PPPoE slot from XIOSYNC (POST /api/v1/pppoe/nodes/acquire)
 *   3. Worker SSH SOCKS5 tunnel (GET /health → ssh_proxy_url)
 *
 * Returns { proxy_url, slot_host_id?, slot_no?, release_fn }
 * Caller MUST call release_fn() after the session ends.
 */
async function resolveProxy({ xiorun_url, session_id, xiosync_url, xiosync_token, log }) {
  const noop = () => Promise.resolve();

  // ── 1. Worker health: get worker_ts_ip + ssh_proxy_url ───────────────────
  let workerHealth = null;
  let workerTsIp   = null;
  let sshProxyUrl  = null;
  try {
    const isHttps = xiorun_url.startsWith('https');
    const { request } = await import(isHttps ? 'node:https' : 'node:http');
    const url = new URL(`${xiorun_url}/health`);
    workerHealth = await new Promise((res, rej) => {
      const req = request({
        hostname: url.hostname, port: url.port || (isHttps ? 443 : 80),
        path: '/health', method: 'GET', timeout: 8000,
      }, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch { rej(new Error('bad json')); } }); });
      req.on('error', rej); req.on('timeout', () => { req.destroy(); rej(new Error('timeout')); });
      req.end();
    });
    // worker Tailscale IP is embedded in cdp_ws_url or can be derived from xiorun_url
    workerTsIp  = new URL(xiorun_url).hostname;
    sshProxyUrl = workerHealth.ssh_proxy_url ?? null;
  } catch (e) {
    log(`[google-signin] ⚠️  Could not reach worker health: ${e.message}`);
  }

  // ── 2. Try PPPoE slot acquisition from XIOSYNC ───────────────────────────
  if (xiosync_url && xiosync_token && workerTsIp) {
    try {
      const isHttps = xiosync_url.startsWith('https');
      const { request } = await import(isHttps ? 'node:https' : 'node:http');
      const url = new URL(`${xiosync_url}/api/v1/pppoe/nodes/acquire`);
      const body = JSON.stringify({ worker_ts_ip: workerTsIp, session_id });
      const slotResult = await new Promise((res, rej) => {
        const req = request({
          hostname: url.hostname, port: url.port || (isHttps ? 443 : 80),
          path: url.pathname, method: 'POST', timeout: 15000,
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
            'Authorization': `Bearer ${xiosync_token}`,
          },
        }, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch { rej(new Error('bad json')); } }); });
        req.on('error', rej);
        req.on('timeout', () => { req.destroy(); rej(new Error('timeout')); });
        req.write(body); req.end();
      });

      if (slotResult.proxy_url) {
        log(`[google-signin] 🔒 PPPoE slot acquired: ${slotResult.proxy_url} (public_ip=${slotResult.public_ip})`);
        const releaseSlot = async () => {
          try {
            const { request: req2 } = await import(isHttps ? 'node:https' : 'node:http');
            const rurl = new URL(`${xiosync_url}/api/v1/pppoe/nodes/${slotResult.host_id}/${slotResult.slot}/assign`);
            await new Promise((res2, rej2) => {
              const r = req2({ hostname: rurl.hostname, port: rurl.port || (isHttps ? 443 : 80),
                path: rurl.pathname, method: 'DELETE', timeout: 8000,
                headers: { 'Authorization': `Bearer ${xiosync_token}` },
              }, resp => { resp.resume(); resp.on('end', res2); });
              r.on('error', rej2); r.on('timeout', () => { r.destroy(); rej2(new Error('timeout')); });
              r.end();
            });
            log(`[google-signin] ✅ PPPoE slot ${slotResult.slot} released`);
          } catch (e) {
            log(`[google-signin] ⚠️  PPPoE slot release failed (non-fatal): ${e.message}`);
          }
        };
        return {
          proxy_url:   slotResult.proxy_url,
          public_ip:   slotResult.public_ip,
          host_id:     slotResult.host_id,
          slot:        slotResult.slot,
          source:      'pppoe',
          release_fn:  releaseSlot,
        };
      }
    } catch (e) {
      log(`[google-signin] ⚠️  PPPoE slot acquisition failed: ${e.message}`);
    }
  }

  // ── 3. Fall back to SSH SOCKS5 tunnel ────────────────────────────────────
  if (sshProxyUrl) {
    log(`[google-signin] 🔒 Using SSH SOCKS5 tunnel: ${sshProxyUrl} (Mac residential IP)`);
    return { proxy_url: sshProxyUrl, public_ip: null, source: 'ssh_tunnel', release_fn: noop };
  }

  // ── 4. No proxy available ────────────────────────────────────────────────
  return { proxy_url: null, public_ip: null, source: 'none', release_fn: noop };
}


// ── HTTP helper (no deps) ─────────────────────────────────────────────────────
function httpPost(xiorun_url, path, body, timeoutMs = 60000) {
  const isHttps = xiorun_url.startsWith('https');
  return import(isHttps ? 'node:https' : 'node:http').then(({ request }) => {
    const url = new URL(`${xiorun_url}${path}`);
    const data = JSON.stringify(body);
    return new Promise((res, rej) => {
      const req = request({
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
        timeout: timeoutMs,
      }, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch(e) { rej(new Error(`Bad JSON: ${d.slice(0,100)}`)); } }); });
      req.on('error', rej);
      req.on('timeout', () => { req.destroy(); rej(new Error(`HTTP POST ${path} timed out`)); });
      req.write(data); req.end();
    });
  });
}


// ── Main workflow export ──────────────────────────────────────────────────────
export async function run(ctx, params = {}) {
  const merged             = { ...params };
  const email              = merged.email;
  const password           = merged.password;
  const totp_secret        = merged.totp_secret;
  const force              = merged.force === true || merged.force === 'true';
  const session_id         = merged.session_id || ctx.sessionId || null;
  const identity_id        = merged.identity_id || null;     // for cascade check + persist-session
  const org_id             = merged.org_id || '00000000-0000-7000-8000-000000000000';
  const xiorun_url         = merged.xiorun_url || null;
  const xiosync_url        = merged.xiosync_url || null;     // for PPPoE slot acquisition
  const xiosync_token      = merged.xiosync_token || null;
  let   proxy_url          = merged.proxy_url  || null;      // explicit override — skip auto-resolve
  let   exit_node_public_ip = merged.exit_node_public_ip || null;

  const _log = (typeof ctx.log === 'function') ? ctx.log.bind(ctx) : msg => process.stderr.write(msg + '\n');

  if (!email)       throw new Error('google-signin: email is required');
  if (!password)    throw new Error('google-signin: password is required');
  if (!totp_secret) throw new Error('google-signin: totp_secret is required');
  if (!xiorun_url)  throw new Error('google-signin: xiorun_url is required (xiorun_agent base URL)');

  // Declare these before proxy resolution — step() is used in that block
  const page = ctx.page;
  let finalResult = null;
  const step = ctx.step ?? ((_, fn) => fn());

  // ── Proxy resolution ──────────────────────────────────────────────────────
  // If proxy_url not explicitly passed, auto-acquire from PPPoE pool or SSH tunnel
  let proxyInfo = { proxy_url: null, release_fn: async () => {} };
  await step('resolve_exit_proxy', async () => {
    if (proxy_url) {
      _log(`[google-signin] 🔒 Using explicit proxy_url: ${proxy_url}`);
      proxyInfo = { proxy_url, public_ip: exit_node_public_ip, source: 'explicit', release_fn: async () => {} };
      return;
    }
    proxyInfo = await resolveProxy({ xiorun_url, session_id, xiosync_url, xiosync_token, log: _log });
    proxy_url = proxyInfo.proxy_url;
    exit_node_public_ip = proxyInfo.public_ip ?? exit_node_public_ip;

    if (!proxy_url) {
      throw new Error(
        'google-signin: No exit node proxy available. ' +
        'Configure a PPPoE host in XIOSYNC (POST /api/v1/pppoe/hosts/register) ' +
        'or ensure the SSH SOCKS5 tunnel is up (check worker /health → ssh_proxy_url).'
      );
    }
    _log(`[google-signin] 🔒 Exit proxy resolved (${proxyInfo.source}): ${proxy_url}`);
  });

  // Cleanup handler — release PPPoE slot on success or failure
  const cleanup = async () => {
    try { await proxyInfo.release_fn(); } catch {}
  };

  try {
    // ── Phase 0: Session cascade check ──────────────────────────────────────
    // 4-level validation (cheapest → most expensive):
    //   L1: Worker local profile dir exists → L2: Live patchright verify
    //   L3: Drive FUSE profile pull → L4: Not found → full login
    if (!force && identity_id && xiorun_url) {
      await step('phase0_cascade_check', async () => {
        _log('[google-signin] Phase 0: session cascade check via worker...');
        try {
          const cascadeResult = await httpPost(xiorun_url, '/session-cascade-check', {
            identity_id, email, proxy_url,
          }, 30_000);
          if (cascadeResult.valid) {
            _log(`[google-signin] ✅ Cascade check passed (level=${cascadeResult.level}) — session still valid`);
            ctx.setResult?.({ success: true, skipped: true, level: cascadeResult.level });
            finalResult = { success: true, skipped: true, level: cascadeResult.level, profile_dir: cascadeResult.profile_dir };
          } else {
            _log(`[google-signin] Cascade check: ${cascadeResult.level} — proceeding to login`);
          }
        } catch (e) {
          _log(`[google-signin] ⚠️  Cascade check error (${e.message}) — proceeding to login`);
        }
      });
      if (finalResult) { await cleanup(); return finalResult; }
    }

    // ── Phase 0b: Fallback browser check (if no identity_id or cascade unavailable)
    if (!force && !finalResult && page) {
      let alreadyLoggedIn = false;
      await step('phase0_browser_check', async () => {
        _log('[google-signin] Phase 0b: browser session check...');
        await page.goto('https://myaccount.google.com/', {
          waitUntil: 'domcontentloaded', timeout: 15000,
        }).catch(() => {});
        await humanSleep(2000, 3000);
        const url   = page.url();
        const title = await page.title().catch(() => '');
        alreadyLoggedIn = url.includes('myaccount.google.com') && !title.toLowerCase().includes('sign in');
        if (alreadyLoggedIn) {
          _log(`[google-signin] ✅ Already logged in: ${url}`);
          ctx.setResult?.({ success: true, skipped: true, url });
          finalResult = { success: true, skipped: true, url };
        } else {
          _log(`[google-signin] Not logged in (${url}) — proceeding to UC stealth login`);
        }
      });
      if (finalResult) { await cleanup(); return finalResult; }
    }

    // ── Phase 1: UC stealth login via xiorun_agent ──────────────────────────
    // UC Chrome runs on the Colab worker using the exit-node proxy.
    // On success: UC Chrome stays OPEN, patchright connects to it via CDP,
    // session registered in xiorun_agent — NO cookie injection needed.
    let ucSessionId, ucCdpWsUrl, ucFinalUrl, ucProfileDir;
    await step('phase1_uc_stealth_login', async () => {
      _log(`[google-signin] Phase 1: UC login via ${xiorun_url}/run-uc-login (proxy=${proxy_url})`);

      const ucResult = await httpPost(xiorun_url, '/run-uc-login', {
        session_id,
        identity_id,
        email,
        password,
        totp_secret,
        proxy_url,
        exit_node_public_ip,
      }, 240_000);  // UC login up to 4 min

      if (!ucResult.ok) {
        // ── HITL: if login failed with a HITL notice, report it ─────────
        if (ucResult.hitl_notice_id) {
          _log(`[google-signin] ⚠️  Login failed with HITL notice ${ucResult.hitl_notice_id} — operator intervention needed`);
        }
        throw new Error(`UC login failed: ${ucResult.error || JSON.stringify(ucResult).slice(0, 200)}`);
      }

      ucSessionId  = ucResult.session_id;
      ucCdpWsUrl   = ucResult.cdp_ws_url;
      ucFinalUrl   = ucResult.final_url;
      ucProfileDir = ucResult.profile_dir || null;
      _log(`[google-signin] ✅ UC login succeeded: ${ucFinalUrl} (session=${ucSessionId})`);
    });


    // ── Phase 2: Navigate UC Chrome to Gmail ────────────────────────────────
    // UC Chrome IS logged in and stays open. Just navigate it to Gmail via
    // POST /navigate — no cookie injection, no cross-browser transfer.
    await step('phase2_navigate_to_gmail', async () => {
      _log('[google-signin] Phase 2: navigating UC Chrome to Gmail...');
      const navResult = await httpPost(xiorun_url, '/navigate', {
        session_id: ucSessionId,
        url: 'https://mail.google.com/mail/u/0/#inbox',
      }, 30_000).catch(e => ({ ok: false, error: e.message }));

      if (!navResult.ok) {
        _log(`[google-signin] ⚠️  Navigate returned: ${JSON.stringify(navResult).slice(0, 100)} — continuing`);
      } else {
        _log(`[google-signin] ✅ Navigated to Gmail: ${navResult.url ?? ''}`);
      }
    });


    // ── Phase 3: Verify via xiorun_agent ────────────────────────────────────
    await step('phase3_verify', async () => {
      // Navigate to myaccount to confirm logged-in state
      const verResult = await httpPost(xiorun_url, '/navigate', {
        session_id: ucSessionId,
        url: 'https://myaccount.google.com/',
      }, 30_000).catch(e => ({ ok: false, error: e.message }));

      const verUrl = verResult.url ?? '';
      if (verResult.ok && verUrl.includes('myaccount.google.com')) {
        _log(`[google-signin] ✅ Login verified: ${verUrl}`);
      } else {
        _log(`[google-signin] ⚠️  Verification inconclusive (${verUrl})`);
      }
    });


    // ── Phase 3.5: Persist session (cookies to vault + profile to Drive) ────
    // Uses the worker's /persist-session endpoint which calls SessionStateIO
    // and ChromeProfileStore under the hood.
    if (identity_id && xiorun_url) {
      await step('phase3_5_persist_session', async () => {
        _log('[google-signin] Phase 3.5: persisting session to vault + Drive...');
        try {
          const persistResult = await httpPost(xiorun_url, '/persist-session', {
            identity_id,
            org_id,
            profile_dir: ucProfileDir,
            page_url: ucFinalUrl,
          }, 60_000);
          if (persistResult.cookie_saved) {
            _log(`[google-signin] ✅ Cookies saved to vault (${persistResult.cookie_count} cookies)`);
          }
          if (persistResult.profile_saved) {
            _log(`[google-signin] ✅ Profile saved to Drive: ${persistResult.profile_key} (${persistResult.profile_size} bytes)`);
          }
          if (!persistResult.cookie_saved && !persistResult.profile_saved) {
            _log('[google-signin] ⚠️  Session persistence incomplete — check worker logs');
          }
        } catch (e) {
          _log(`[google-signin] ⚠️  Persist-session error: ${e.message} — login succeeded but session not saved`);
        }
      });
    }


    // ── Post-success: push Chrome profile to Drive (belt-and-suspenders fallback) ──
    await pushProfileToStorage({ session_id: ucSessionId, identity_id, profile_dir: ucProfileDir, xiorun_url, log: _log });

    finalResult = {
      success:     true,
      engine:      'uc',
      final_url:   ucFinalUrl,
      session_id:  ucSessionId,
      cdp_ws_url:  ucCdpWsUrl,
      proxy_source: proxyInfo.source,
    };
    ctx.setResult?.(finalResult);
    return finalResult;

  } finally {
    await cleanup();
  }
}


// ── Subprocess entrypoint ─────────────────────────────────────────────────────
// Called by XIOSYNC ScriptRunner:
//   node google-signin.mjs --context=<json> [--session-id=<uuid>]
// Connects to the live browser via patchright CDP, builds ctx, calls run().
// Outputs a single JSON result line to stdout (ScriptRunner reads last JSON line).

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  (async () => {
    // ── Parse CLI args ──────────────────────────────────────────────────────
    const ctxArg = process.argv.find(a => a.startsWith('--context='))?.slice(10) ?? '{}';
    const sidArg = process.argv.find(a => a.startsWith('--session-id='))?.slice(13) ?? null;
    let ctxData;
    try {
      ctxData = JSON.parse(ctxArg);
    } catch (e) {
      console.log(JSON.stringify({ status: 'error', error: `Invalid --context JSON: ${e.message}` }));
      process.exit(1);
    }

    const cdp_ws_url = ctxData.cdp_ws_url ?? null;
    const _log = msg => process.stderr.write(`[${new Date().toISOString().slice(11,19)}] ${msg}\n`);

    // ── Connect to live browser via patchright CDP ──────────────────────────
    let browser = null;
    let page    = null;

    if (cdp_ws_url) {
      try {
        _log(`Connecting patchright to CDP: ${cdp_ws_url}`);
        const { chromium } = await import('patchright');
        // patchright connectOverCDP expects an HTTP endpoint, not ws://
        const cdpHttpUrl = cdp_ws_url.replace(/^ws:\/\//, 'http://').replace(/^wss:\/\//, 'https://');
        browser = await chromium.connectOverCDP(cdpHttpUrl);
        const ctx0 = browser.contexts()[0] ?? await browser.newContext();
        page = ctx0.pages()[0] ?? await ctx0.newPage();
        _log(`CDP connected. Page URL: ${page.url()}`);
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', error: `CDP connect failed: ${e.message}` }));
        process.exit(1);
      }
    } else {
      _log('WARNING: no cdp_ws_url in context — ctx.page will be null (Phase 0 + 2 + 3 skipped)');
    }

    // ── Build ctx ───────────────────────────────────────────────────────────
    let _result = null;
    const ctx = {
      page,
      sessionId: ctxData.session_id ?? sidArg ?? 'unknown',
      jobId:     ctxData.job_id     ?? 'nojob',
      log:       _log,
      step: async (name, fn) => {
        _log(`[step:${name}] START`);
        const t0 = Date.now();
        try {
          await fn();
          _log(`[step:${name}] DONE (${Date.now() - t0}ms)`);
        } catch (e) {
          _log(`[step:${name}] FAILED (${Date.now() - t0}ms): ${e.message}`);
          throw e;
        }
      },
      hitl: async (msg, details = {}) => {
        // Create HITL notice on the worker agent — operator can resume via /hitl/{id}/resume
        const _xiorun = ctxData.xiorun_url;
        if (_xiorun) {
          try {
            const http = await import('node:http');
            const noticeReq = JSON.stringify({
              organization_id: ctxData.org_id || '00000000-0000-7000-8000-000000000000',
              session_id: ctxData.session_id || 'workflow',
              challenge_type: details.challenge_type || 'hitl_workflow',
              message: msg,
              instructions: details.instructions || 'Check XIOVIEW and resume when ready.',
            });
            const url = new URL(_xiorun);
            await new Promise((res, rej) => {
              const r = http.request({ hostname: url.hostname, port: url.port, path: '/hitl/create', method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(noticeReq) },
                timeout: 10000 }, resp => { let d = ''; resp.on('data', c => d += c); resp.on('end', () => {
                  try { const j = JSON.parse(d); _log(`[HITL] Notice created: ${j.id || d}`); } catch { _log(`[HITL] ${d}`); }
                  res();
                });
              });
              r.on('error', e => { _log(`[HITL] create error: ${e.message}`); rej(e); });
              r.write(noticeReq); r.end();
            });
          } catch (e) { _log(`[HITL] Failed: ${e.message}`); }
        }
        throw new Error(`HITL_REQUIRED: ${msg}`);
      },
      setResult: r => { _result = r; },
      isCancelled: () => false,
    };

    // ── Execute workflow ─────────────────────────────────────────────────────
    try {
      const res = await run(ctx, ctxData);
      console.log(JSON.stringify({ status: 'success', result: _result ?? res ?? {} }));
    } catch (e) {
      console.log(JSON.stringify({ status: 'error', error: e.message, detail: e.stack?.slice(0, 500) }));
      process.exitCode = 1;
    } finally {
      // Disconnect without closing the remote browser (it stays alive)
      if (browser) { try { await browser.close(); } catch {} }
    }
  })();
}
