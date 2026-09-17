// ─── Stealth Runner ───────────────────────────────────────────────────────
// Decoupled bot-bypass engine. Owns all stealth machinery so that individual
// workflows stay clean and contain only high-level orchestration logic.
//
// Exports:
//   runStealthSidecar({ email, password, totp_secret, sessionPath,
//                       screenshotDir, log, workflowId, domain, jobId })
//     → { success: boolean, engine: string|null }
//
//   killSidecar(jobId)  → boolean (true if a process was killed)
//
//   handle2FAViaCDP({ debugPort, totpSecret, sessionPath, log, screenshotDir })
//     → void  (throws on fatal error)
//
//   generateTOTP(secret)  → '123456'
//   base32Decode(encoded) → Buffer

import { createHmac }                from 'node:crypto';
import { existsSync, readFileSync,
         writeFileSync, unlinkSync } from 'node:fs';
import { fileURLToPath }             from 'node:url';
import { resolve as _resolve, dirname as _dirname } from 'node:path';
import { createLogger }              from '../utils/logger.mjs';

const log = createLogger('stealth-runner');

// ── Locate xio-browser root ────────────────────────────────────────────────
// Works regardless of whether this file is loaded from the git repo or from
// an xio-mesh/workflows/ copy on Drive.
const _thisDir  = _dirname(fileURLToPath(import.meta.url));
// When loaded from src/core/ inside the repo, root is two levels up.
const _xiobr = _resolve(_thisDir, '../..');

// ── Sidecar Process Registry ───────────────────────────────────────────────
// Maps jobId → spawned python3 child process.  Populated by runStealthSidecar,
// cleared when the process exits.  Consumed by killSidecar() so that
// cancelJob() in job-manager.mjs can force-terminate stuck chrome/python.
const _sidecarProcessMap = new Map();

/**
 * Force-kill the python sidecar (and its child chrome process via SIGKILL)
 * for the given jobId.  Returns true if a live process was found and killed.
 */
export function killSidecar(jobId) {
  const child = _sidecarProcessMap.get(jobId);
  if (!child || child.exitCode !== null || child.killed) return false;
  try {
    // SIGKILL so chrome subprocesses don't linger
    process.kill(-child.pid, 'SIGKILL');
  } catch (_) {
    try { child.kill('SIGKILL'); } catch (_2) {}
  }
  _sidecarProcessMap.delete(jobId);
  log.info(`[stealth-runner] 🔪 Force-killed sidecar (jobId=${jobId} pid=${child.pid})`);
  return true;
}

// ── TOTP ──────────────────────────────────────────────────────────────────

export function base32Decode(encoded) {
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = 0, value = 0;
  const output = [];
  for (const ch of encoded.toUpperCase().replace(/=+$/, '')) {
    value = (value << 5) | alpha.indexOf(ch);
    bits += 5;
    if (bits >= 8) { output.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return Buffer.from(output);
}

export function generateTOTP(secret) {
  const key     = base32Decode(secret);
  const counter = Math.floor(Date.now() / 30000);
  const buf     = Buffer.alloc(8);
  buf.writeBigInt64BE(BigInt(counter));
  const hmac   = createHmac('sha1', key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0xf;
  return ((hmac.readUInt32BE(offset) & 0x7fffffff) % 1000000).toString().padStart(6, '0');
}

// ── Stealth Sidecar Runner ─────────────────────────────────────────────────
// Launches google_stealth_login.py with the ESR-ranked engine order.
// When the Python sidecar signals a 2FA challenge via a temp file,
// handle2FAViaCDP() takes over, resolves it, and writes a resume token.

/**
 * @param {object} opts
 * @param {string} opts.email
 * @param {string} opts.password
 * @param {string} opts.totp_secret
 * @param {string} opts.sessionPath   - where the sidecar writes its session JSON
 * @param {string} opts.screenshotDir
 * @param {Function} opts.log         - ctx.log or console.log
 * @param {string} [opts.workflowId]
 * @param {string} [opts.domain]
 * @param {Function} [opts.getRankedEngines]   - from engine-selector.mjs (injected)
 * @param {Function} [opts.recordEngineResult] - from engine-selector.mjs (injected)
 */
export async function runStealthSidecar({
  email, password, totp_secret, sessionPath, screenshotDir, log: _log,
  workflowId = 'google-signin',
  domain     = 'accounts.google.com',
  profileDir = null,
  jobId      = null,          // passed by job-manager so killSidecar() can find the process
  getRankedEngines   = () => ['uc'],
  recordEngineResult = () => {},
}) {
  const info = msg => { _log?.(msg); log.info(msg); };

  const sidecarPath = `${_xiobr}/colab/google_stealth_login.py`;
  if (!existsSync(sidecarPath)) {
    info(`[stealth-runner] ⚠️  Sidecar not found at ${sidecarPath} — aborting`);
    return { success: false, engine: null };
  }

  // uc is first in ESR order (proven engine); ESR adjusts dynamically after each run
  const engines = getRankedEngines(workflowId, domain);
  info(`[stealth-runner] 🎯 ESR engine order: ${engines.join(' → ')}`);

  // Clean up stale signal files from any previous run to prevent race conditions
  const _uid = jobId || Date.now();
  const SIGNAL_FILE = `/tmp/xio_uc_2fa_signal_${_uid}.json`;
  const RESUME_FILE = `/tmp/xio_uc_2fa_resume_${_uid}.json`;
  if (existsSync(SIGNAL_FILE)) { try { unlinkSync(SIGNAL_FILE); } catch (_) {} }
  if (existsSync(RESUME_FILE)) { try { unlinkSync(RESUME_FILE); } catch (_) {} }

  const args = [
    sidecarPath,
    '--email',          email,
    '--password',       password,
    '--totp_secret',    totp_secret || '',
    '--socks5',         'socks5://127.0.0.1:1055',
    '--output',         sessionPath,
    '--engines',        engines.join(','),
    '--screenshot_dir', screenshotDir,
    '--workflow_id',    workflowId,
    '--domain',         domain,
    '--signal_file',    SIGNAL_FILE,
    '--resume_file',    RESUME_FILE,
    ...(profileDir ? ['--profile_dir', profileDir] : []),
  ];

  if (profileDir) info(`[stealth-runner] 💾 Profile dir: ${profileDir}`);

  info(`[stealth-runner] 🦊 Launching stealth sidecar (${sidecarPath})...`);

  const { spawn } = await import('node:child_process');
  let stdoutBuf = '', stderrBuf = '';
  const child = spawn('python3', args, { stdio: ['ignore', 'pipe', 'pipe'], detached: false });
  child.stdout.on('data', c => { stdoutBuf += c.toString(); });
  child.stderr.on('data', c => { stderrBuf += c.toString(); });
  // Register for force-kill via killSidecar(jobId)
  if (jobId) {
    _sidecarProcessMap.set(jobId, child);
    child.on('exit', () => _sidecarProcessMap.delete(jobId));
  }

  const _start = Date.now();

  await new Promise((resolve) => {
    let devtoolsDone = false;
    const timer = setInterval(async () => {
      if (child.exitCode !== null || child.killed) { clearInterval(timer); return resolve(); }
      // Hard timeout: 5 minutes
      if (Date.now() - _start > 300_000) {
        info('[stealth-runner] ⏱ Sidecar timeout — killing');
        child.kill('SIGTERM');
        clearInterval(timer); return resolve();
      }
      // 2FA signal detection: Python wrote /tmp/xio_uc_2fa_signal.json
      if (!devtoolsDone && existsSync(SIGNAL_FILE)) {
        devtoolsDone = true;
        let sig = {};
        try { sig = JSON.parse(readFileSync(SIGNAL_FILE, 'utf8')); } catch (_) {}
        info(`[stealth-runner] 🔐 2FA signal! Chrome DevTools on port ${sig.debug_port || 9300}`);
        info(`[stealth-runner] 🌐 Challenge URL: ${sig.url}`);
        try {
          await handle2FAViaCDP({
            debugPort: sig.debug_port || 9300,
            totpSecret: totp_secret, sessionPath,
            log: _log, screenshotDir,
            resumeFile: RESUME_FILE,   // ← required: lets CDP handler signal Python when bframe appears
          });
          // Preserve 'bframe_open'/'checkbox_done' if handle2FAViaCDP yielded to Python
          let _skipDone = false;
          if (existsSync(RESUME_FILE)) {
            try {
              const _rd = JSON.parse(readFileSync(RESUME_FILE, 'utf8'));
              if (_rd.status === 'bframe_open' || _rd.status === 'checkbox_done') {
                _skipDone = true;
                info('[stealth-runner] 📡 CDP yielded to Python audio solver — preserving RESUME_FILE status');
              }
            } catch {}
          }
          if (!_skipDone) {
            writeFileSync(RESUME_FILE, JSON.stringify({ status: 'done', ts: Date.now() }));
            info('[stealth-runner] ✅ CDP 2FA complete — sidecar resumed');
          }
        } catch (e) {
          info(`[stealth-runner] ⚠️ CDP 2FA error: ${e.message} — using inline fallback`);
          writeFileSync(RESUME_FILE, JSON.stringify({ status: 'error', msg: e.message }));
        }
      }
    }, 2000);
    child.on('close', () => { clearInterval(timer); resolve(); });
  });

  // Relay sidecar logs and update ESR
  for (const line of stdoutBuf.split('\n')) {
    if (!line.trim()) continue;
    const esrMatch = line.match(/^__ESR_RESULT__ (\w+) (success|fail)$/);
    if (esrMatch) {
      const [, engine, outcome] = esrMatch;
      recordEngineResult(workflowId, domain, engine, outcome === 'success');
      info(`[stealth-runner] ESR updated: ${engine} → ${outcome}`);
      continue;
    }
    info(`  ${line}`);
  }

  if (stderrBuf.trim()) {
    info(`[stealth-runner] sidecar stderr: ${stderrBuf.slice(0, 600)}`);
  }

  // Parse final result JSON from last stdout line
  const jsonLine = stdoutBuf.trim().split('\n').reverse()
    .find(l => l.trim().startsWith('{'));
  if (jsonLine) {
    try {
      const parsed = JSON.parse(jsonLine);
      if (parsed.success) {
        info(`[stealth-runner] ✅ Sidecar succeeded via engine: ${parsed.engine}`);
        return { success: true, engine: parsed.engine };
      }
    } catch (_) {}
  }

  // ── Structured failure classification ──────────────────────────────────────
  // Inspect stdout to identify WHY the sidecar failed so the caller (google-signin)
  // can escalate recoverable failures to HITL instead of hard-failing the job.
  //
  // failureType values:
  //   'totp_rejected'        — TOTP code was entered but Google rejected it
  //   'verification_failed'  — Login flow completed but session verify found wrong account
  //   'totp_input_not_found' — 2FA page appeared but TOTP input could not be located
  //   'all_engines_failed'   — No engine could log in (may still be recoverable via HITL)
  //
  // hitl: true means the failure is recoverable by human intervention (not a code bug).

  let failureType = 'all_engines_failed';
  let hitl = false;

  if (/Wrong number of digits/i.test(stdoutBuf) || /totp.*rejected|invalid.*code|incorrect.*code/i.test(stdoutBuf)) {
    failureType = 'totp_rejected';
    hitl = true;
    info('[stealth-runner] ⚠️  Failure type: totp_rejected — TOTP digits were rejected by Google');
  } else if (/Verification failed.*account\/about|account\/about.*Verification failed/i.test(stdoutBuf) ||
             /\[uc\] ❌ Verification failed/i.test(stdoutBuf)) {
    failureType = 'verification_failed';
    hitl = true;
    info('[stealth-runner] ⚠️  Failure type: verification_failed — logged in but session verify failed');
  } else if (/No TOTP input found/i.test(stdoutBuf)) {
    failureType = 'totp_input_not_found';
    hitl = true;
    info('[stealth-runner] ⚠️  Failure type: totp_input_not_found — 2FA page but no input located');
  } else {
    info('[stealth-runner] ❌ Failure type: all_engines_failed');
  }

  return { success: false, engine: null, failureType, hitl };
}


// ── CDP 2FA Handler ──────────────────────────────────────────────────────────
// Connects directly to uc Chrome's remote-debugging port using native Node.js
// WebSocket frames (RFC 6455) without any npm packages.

export async function handle2FAViaCDP({ debugPort, totpSecret, sessionPath, log: _log, screenshotDir, resumeFile }) {
  const http  = await import('node:http');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const info  = msg => { _log?.(msg); log.info(msg); };

  // ── Step 1: Wait for Chrome's debug HTTP server ────────────────────────────
  info(`[cdp-2fa] Polling Chrome debug port ${debugPort} (up to 30s)...`);
  let tabs = null;
  for (let attempt = 1; attempt <= 15; attempt++) {
    await sleep(2000);
    try {
      tabs = await new Promise((res, rej) => {
        const req = http.get(
          { host: '127.0.0.1', port: debugPort, path: '/json', timeout: 3000 },
          r => {
            let d = '';
            r.on('data', c => d += c);
            r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
          }
        );
        req.on('error', rej);
        req.on('timeout', () => { req.destroy(); rej(new Error('timeout')); });
      });
      if (tabs?.length > 0) {
        info(`[cdp-2fa] ✅ Chrome debug ready after attempt ${attempt} — ${tabs.length} tab(s)`);
        break;
      }
    } catch (e) {
      info(`[cdp-2fa] Attempt ${attempt}/15: ${e.message}`);
      tabs = null;
    }
  }

  if (!tabs) throw new Error(`Chrome debug port ${debugPort} not reachable after 30s`);

  const tab = tabs.find(t => t.type === 'page') || tabs[0];
  if (!tab?.webSocketDebuggerUrl) throw new Error(`No page tab on port ${debugPort}`);
  info(`[cdp-2fa] Tab: ${tab.url?.slice(0, 70)}`);

  // ── Step 2: CDP over native WebSocket (RFC 6455) ───────────────────────────
  const wsPath = new URL(tab.webSocketDebuggerUrl).pathname;

  const cdpCall = (method, params = {}) => new Promise(resolve => {
    const msgId   = (Math.random() * 1e9) | 0;
    const body    = JSON.stringify({ id: msgId, method, params });
    const payload = Buffer.from(body, 'utf8');
    const maskKey = Buffer.allocUnsafe(4);
    for (let i = 0; i < 4; i++) maskKey[i] = (Math.random() * 256) | 0;

    // RFC 6455 frame header — three length regions:
    //   < 126 bytes: 1 byte length
    //   < 65536 bytes: 2 bytes with 0xFE marker
    //   ≥ 65536 bytes: 8 bytes with 0xFF marker (Fix #6)
    let header;
    if (payload.length < 126) {
      header = Buffer.from([0x81, 0x80 | payload.length, ...maskKey]);
    } else if (payload.length < 65536) {
      header = Buffer.from([
        0x81, 0xFE,
        (payload.length >> 8) & 0xFF, payload.length & 0xFF,
        ...maskKey,
      ]);
    } else {
      // 64-bit extended payload length (big-endian, high word first)
      const high = Math.floor(payload.length / 0x100000000);
      const low  = payload.length >>> 0;
      header = Buffer.from([
        0x81, 0xFF,
        (high >> 24) & 0xFF, (high >> 16) & 0xFF, (high >> 8) & 0xFF, high & 0xFF,
        (low  >> 24) & 0xFF, (low  >> 16) & 0xFF, (low  >> 8) & 0xFF, low  & 0xFF,
        ...maskKey,
      ]);
    }

    // XOR-mask the payload (required for client→server frames)
    const masked = Buffer.allocUnsafe(payload.length);
    for (let i = 0; i < payload.length; i++) masked[i] = payload[i] ^ maskKey[i % 4];

    const frame = Buffer.concat([header, masked]);

    const req = http.request({
      host: '127.0.0.1', port: debugPort, path: wsPath, method: 'GET',
      headers: {
        Upgrade:                  'websocket',
        Connection:               'Upgrade',
        'Sec-WebSocket-Key':      Buffer.from(`xio-cdp-${msgId}`).toString('base64'),
        'Sec-WebSocket-Version':  '13',
        'Sec-WebSocket-Protocol': 'chat',
      },
    });

    req.on('upgrade', (_res, socket) => {
      socket.write(frame);
      let chunks = [];
      socket.on('data', chunk => {
        chunks.push(chunk);
        const data = Buffer.concat(chunks);
        if (data.length < 2) return;
        const opcode = data[0] & 0x0F;
        if (opcode !== 0x01 && opcode !== 0x00) return;  // not text/continuation
        let payloadLen = data[1] & 0x7F;
        let offset = 2;
        if (payloadLen === 126) { payloadLen = data.readUInt16BE(2); offset = 4; }
        else if (payloadLen === 127) { payloadLen = Number(data.readBigUInt64BE(2)); offset = 10; }
        if (data.length < offset + payloadLen) return;  // incomplete frame
        try {
          const msg = JSON.parse(data.slice(offset, offset + payloadLen).toString('utf8'));
          if (msg.id === msgId) { socket.destroy(); resolve(msg); }
        } catch { /* wait for more data */ }
      });
      socket.on('error', () => resolve(null));
      setTimeout(() => { socket.destroy(); resolve(null); }, 12000);
    });

    req.on('error', (e) => { info(`[cdp-2fa] WS error: ${e.message}`); resolve(null); });
    req.end();
  });

  const cdpEvaluate = async (expression) => {
    const msg = await cdpCall('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    return msg?.result?.result?.value ?? msg;
  };

  // ── Step 3: 2FA navigation via CDP ────────────────────────────────────────
  info('[cdp-2fa] Injecting 2FA navigation script...');
  await sleep(1500);

  // ── reCAPTCHA branch ───────────────────────────────────────────────────────
  // When Google shows a reCAPTCHA at /v3/signin/challenge/recaptcha, the normal
  // TOTP flow must NOT run ("Try another way" on the reCAPTCHA page triggers
  // account rejection). Instead:
  //   1. Find the reCAPTCHA anchor iframe's viewport position
  //   2. Click the checkbox via Input.dispatchMouseEvent (cross-iframe)
  //   3. If a challenge bframe appears, click Buster's solve button
  //   4. Poll up to 90s for the URL to leave the reCAPTCHA page
  if (tab.url?.includes('/challenge/recaptcha') || tab.url?.includes('recaptcha')) {
    info('[cdp-2fa] 🤖 reCAPTCHA challenge page detected — using checkbox+Buster flow');

    // 3-rc-a: Get anchor iframe viewport rect
    const anchorRect = await cdpEvaluate(`
(function() {
  const f = [...document.querySelectorAll('iframe')]
    .find(f => (f.src || '').includes('recaptcha') && (f.src || '').includes('anchor'));
  if (!f) return null;
  const r = f.getBoundingClientRect();
  return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
})()`);
    info(`[cdp-2fa] reCAPTCHA anchor iframe rect: ${JSON.stringify(anchorRect)}`);

    if (anchorRect?.x != null) {
      // The checkbox sits at ~(28px, 17px) inside the anchor iframe
      const cbX = anchorRect.x + 28;
      const cbY = anchorRect.y + 17;
      info(`[cdp-2fa] Clicking reCAPTCHA checkbox at viewport (${cbX}, ${cbY})...`);
      await cdpCall('Input.dispatchMouseEvent', { type: 'mousePressed', x: cbX, y: cbY, button: 'left', clickCount: 1, modifiers: 0 });
      await sleep(120);
      await cdpCall('Input.dispatchMouseEvent', { type: 'mouseReleased', x: cbX, y: cbY, button: 'left', clickCount: 1, modifiers: 0 });
      info('[cdp-2fa] ☑️  reCAPTCHA checkbox click dispatched');
      await sleep(3000);
    } else {
      info('[cdp-2fa] ⚠️  reCAPTCHA anchor iframe not found — checkbox click skipped');
    }

    // 3-rc-b: Check if a challenge bframe appeared (audio challenge required)
    let bframeDetected = false;
    for (let attempt = 0; attempt < 5; attempt++) {
      const bframeVisible = await cdpEvaluate(`
(function() {
  const f = [...document.querySelectorAll('iframe')]
    .find(f => (f.src || '').includes('recaptcha') && (f.src || '').includes('bframe'));
  if (!f) return false;
  const r = f.getBoundingClientRect();
  return r.width > 50;
})()`);
      if (bframeVisible) {
        bframeDetected = true;
        info('[cdp-2fa] 🔊 reCAPTCHA audio challenge bframe detected — signaling Python audio solver...');
        // Signal Python to handle the audio challenge (it has switch_to.frame access)
        if (resumeFile) {
          const { writeFileSync: _wf } = await import('node:fs');
          _wf(resumeFile, JSON.stringify({ status: 'checkbox_done', bframe: true, ts: Date.now() }));
          info('[cdp-2fa] 📡 Wrote RESUME_FILE {status:"checkbox_done"} — yielding to Python audio solver');
        } else {
          info('[cdp-2fa] ⚠️  resumeFile not provided — Python will use inline fallback');
        }
        return; // Python will switch_to.frame(bframe) and solve audio
      }
      info(`[cdp-2fa] bframe not visible yet (attempt ${attempt + 1}/5) — retrying in 1.5s`);
      await sleep(1500);
    }

    if (!bframeDetected) {
      info('[cdp-2fa] ✅ reCAPTCHA checkbox passed — no audio challenge bframe appeared');
    }

    // Wait for the page to navigate away from reCAPTCHA (max 15s)
    info('[cdp-2fa] Waiting for URL to leave challenge/recaptcha after Buster solve...');
    for (let attempt = 0; attempt < 10; attempt++) {
      const currentUrl = await cdpEvaluate('window.location.href');
      if (currentUrl && !currentUrl.includes('challenge/recaptcha') && !currentUrl.includes('recaptcha')) {
        info(`[cdp-2fa] ✅ reCAPTCHA cleared, new URL: ${currentUrl.slice(0, 80)}`);
        break;
      }
      await sleep(1500);
    }
    
    // Allow the code to fall through to the standard TOTP handler below
    info('[cdp-2fa] Falling through to standard TOTP flow...');
  }

  if (totpSecret) {
    // ── 3a: Always check for device-push / phone-prompt and dismiss it ──────
    // Google shows "Check your [device]" by default. We must click
    // "Try another way" to get the full options list including Authenticator.
    const tryAnotherWayScript = `
(async function() {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const dispatch = el => {
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(ev =>
      el.dispatchEvent(new MouseEvent(ev, {bubbles:true,cancelable:true,view:window}))
    );
  };
  // Detect device-push screen: looks for "Check your" text or challenge type 14/16
  const devicePrompt = [...document.querySelectorAll('h1,h2,[data-challengetype]')]
    .find(e => /check your|a notification|tap yes/i.test(e.innerText || '')
           || ['14','15','16'].includes(e.getAttribute?.('data-challengetype') || ''));
  const taw = [...document.querySelectorAll('button,div[role="button"],a,span,[role="link"]')]
    .find(e => /try another way|more options|try a different/i.test((e.innerText || '').trim()));
  if (devicePrompt || taw) {
    if (taw) { dispatch(taw); await sleep(3000); return { clicked: true, found: 'try_another_way' }; }
    return { clicked: false, found: 'device_prompt_no_taw_link' };
  }
  return { clicked: false, found: 'no_device_prompt' };
})()`;
    const tawResult = await cdpEvaluate(tryAnotherWayScript);
    info(`[cdp-2fa] Try-another-way result: ${JSON.stringify(tawResult)}`);
    if (tawResult?.clicked) {
      await sleep(2500); // wait for the options list to load
      // 📸 Capture the 2FA options list (most important decision screen)
      try {
        const _r1 = await cdpCall('Page.captureScreenshot', { format: 'jpeg', quality: 80 });
        const _sc1 = _r1?.result?.data;
        if (_sc1 && screenshotDir) {
          const { writeFileSync } = await import('fs');
          writeFileSync(`${screenshotDir}/cdp_2fa_options.jpg`, Buffer.from(_sc1, 'base64'));
          info('[cdp-2fa] 📸 cdp_2fa_options.jpg saved');
        }
      } catch (_sce) { info(`[cdp-2fa] options screenshot failed: ${_sce.message}`); }
    } else if (tawResult?.found === 'device_prompt_no_taw_link') {
      // Device prompt is showing but "Try another way" is absent — HITL required
      info('[cdp-2fa] ⚠️  Device prompt present but no bypass link — escalating to HITL');
      // Write a signal file that callers (stealth-runner, google-signin) can read
      const { writeFileSync } = await import('fs');
      const signalPath = '/tmp/xio_uc_2fa_signal.json';
      writeFileSync(signalPath, JSON.stringify({
        status: 'hitl_required',
        reason: 'device_prompt_no_bypass',
        ts: new Date().toISOString(),
      }));
      // If a ctx with hitl support was passed in, escalate
      if (typeof ctx?.hitl === 'function') {
        await ctx.hitl(
          'Google device verification required during stealth login — no bypass link found.',
          { instructions: 'Check your registered device, tap "Yes" on the Google prompt, then tap the number shown. Tell the agent when done.' }
        );
        // After resume: signal file is stale — delete it
        try { require('fs').unlinkSync(signalPath); } catch {}
      } else {
        // No ctx.hitl available — throw structured error for caller to handle
        throw Object.assign(
          new Error('[HITL] Device verification required — no bypass available'),
          { hitl: true, reason: 'device_prompt_no_bypass' }
        );
      }
    }

    // ── 3b: Select Google Authenticator from the options list ───────────────
    const selectAuthScript = `
(async function() {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const dispatch = el => {
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(ev =>
      el.dispatchEvent(new MouseEvent(ev, {bubbles:true,cancelable:true,view:window}))
    );
  };
  // Google Authenticator challenge type is 6
  const auth6 = document.querySelector('[data-challengetype="6"]');
  if (auth6) { dispatch(auth6); await sleep(2500); return { clicked: true, method: 'challengetype_6' }; }
  // Fallback: find authenticator/auth app option by text
  const authEl = [...document.querySelectorAll('div[role="link"],li,button,div[role="option"],div[role="listitem"]')]
    .find(e => /authenticator|auth app|google auth/i.test(e.innerText || ''));
  if (authEl) { dispatch(authEl); await sleep(2500); return { clicked: true, method: 'text_match' }; }
  return { clicked: false, url: location.href };
})()`;
    const authResult = await cdpEvaluate(selectAuthScript);
    info(`[cdp-2fa] Select authenticator result: ${JSON.stringify(authResult)}`);
    if (authResult?.clicked) {
      await sleep(2000); // wait for TOTP input to appear
    } // end if(authResult?.clicked)

    // ── 3c: Generate fresh TOTP NOW (after navigation, not before) ──────────
    const code = generateTOTP(totpSecret);
    info(`[cdp-2fa] Fresh TOTP (post-nav): ${code}`);

    // ── 3d: Fill TOTP code into the input (do NOT click Next yet) ──────────
    const fillCodeScript = `
(async function() {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const dispatch = el => {
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(ev =>
      el.dispatchEvent(new MouseEvent(ev, {bubbles:true,cancelable:true,view:window}))
    );
  };
  const all = [...document.querySelectorAll('input:not([type="hidden"])')];
  let inp = null;
  for (const i of all) {
    const r = i.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      const a = (i.id+' '+i.name+' '+(i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
      if (a.includes('code')||a.includes('totp')||a.includes('pin')||i.type==='tel'||i.type==='number') { inp=i; break; }
    }
  }
  if (!inp) inp = all.find(i => { const r=i.getBoundingClientRect(); return r.width>0&&r.height>0&&['tel','number','text'].includes(i.type); });
  if (!inp) return { ok:false, reason:'no_input', url:location.href, inputs:all.length };
  dispatch(inp); inp.focus(); inp.value = '';
  for (const ch of '${code}') {
    inp.value += ch;
    inp.dispatchEvent(new Event('input',  {bubbles:true}));
    inp.dispatchEvent(new Event('change', {bubbles:true}));
    await sleep(65 + Math.random()*85);
  }
  return { ok:true, code:'${code}', filled:inp.value, url:location.href };
})()`;
    const fillResult = await cdpEvaluate(fillCodeScript);
    info(`[cdp-2fa] TOTP fill result: ${JSON.stringify(fillResult)}`);

    // ── 3e: Screenshot AFTER fill, BEFORE Next click ────────────────────
    // This captures the 6-digit code visible in the input field.
    await sleep(200); // tiny settle so React controlled input renders the value
    try {
      const _r2 = await cdpCall('Page.captureScreenshot', { format: 'jpeg', quality: 80 });
      const _sc2 = _r2?.result?.data;
      if (_sc2 && screenshotDir) {
        const { writeFileSync } = await import('fs');
        writeFileSync(`${screenshotDir}/cdp_2fa_totp_input.jpg`, Buffer.from(_sc2, 'base64'));
        info('[cdp-2fa] 📸 cdp_2fa_totp_input.jpg saved (code filled, pre-Next)');
      }
    } catch (_sce) { info(`[cdp-2fa] totp_input screenshot failed: ${_sce.message}`); }

    // ── 3f: Click Next / submit ─────────────────────────────────────────
    if (fillResult?.ok) {
      const clickNextScript = `
(async function() {
  const dispatch = el => {
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(ev =>
      el.dispatchEvent(new MouseEvent(ev, {bubbles:true,cancelable:true,view:window}))
    );
  };
  const inp = document.querySelector('input[type="tel"],input[type="number"],input[type="text"]');
  const nb = document.querySelector(
    '#totpNext,#idvPreregisteredPhoneNext,[jsname="LgbsSe"],button[type="submit"],' +
    'button[aria-label*="Next"],button[aria-label*="Verify"]'
  );
  if (nb) { dispatch(nb); return { clicked: true, method: 'button' }; }
  if (inp) {
    ['keydown','keypress','keyup'].forEach(k =>
      inp.dispatchEvent(new KeyboardEvent(k,{key:'Enter',code:'Enter',keyCode:13,bubbles:true}))
    );
    return { clicked: true, method: 'enter' };
  }
  return { clicked: false };
})()`;
      const clickResult = await cdpEvaluate(clickNextScript);
      info(`[cdp-2fa] TOTP Next click: ${JSON.stringify(clickResult)}`);
    }

    // ── 3g: Wait for page to leave /challenge/, then wait for full load ──
    const _waitStart = Date.now();
    while (Date.now() - _waitStart < 8000) {
      await sleep(500);
      try {
        const _urlNow = await cdpEvaluate('window.location.href');
        if (_urlNow && !_urlNow.includes('/challenge/')) {
          info(`[cdp-2fa] ✅ Left challenge page: ${_urlNow.slice(0, 80)}`);
          break;
        }
      } catch { break; }
    }
    // Wait for destination page to fully render (readyState===complete) before screenshot
    const _loadStart = Date.now();
    while (Date.now() - _loadStart < 5000) {
      await sleep(400);
      try {
        const _ready = await cdpEvaluate('document.readyState');
        if (_ready === 'complete') { await sleep(600); break; } // extra 600ms for paint
      } catch { break; }
    }
    // 📸 Capture post-TOTP fully-loaded destination page
    try {
      const _r3 = await cdpCall('Page.captureScreenshot', { format: 'jpeg', quality: 80 });
      const _sc3 = _r3?.result?.data;
      if (_sc3 && screenshotDir) {
        const { writeFileSync } = await import('fs');
        writeFileSync(`${screenshotDir}/cdp_2fa_totp_submitted.jpg`, Buffer.from(_sc3, 'base64'));
        info('[cdp-2fa] 📸 cdp_2fa_totp_submitted.jpg saved (fully loaded post-TOTP page)');
      }
    } catch (_sce) { info(`[cdp-2fa] totp_submitted screenshot failed: ${_sce.message}`); }
  } // end if(totpSecret)


  // Post-login prompts (Skip / Cancel / Not Now)
  const skipped = await cdpEvaluate(`
    (function(){
      var all = document.querySelectorAll('button,div[role="button"],div[role="link"],a,span');
      for (var i = 0; i < all.length; i++) {
        var t = (all[i].innerText || '').toLowerCase();
        if (t.includes('cancel') || t.includes('not now') || t.includes('skip')) {
          all[i].scrollIntoView({block:'center'}); all[i].click(); return true;
        }
      }
      return false;
    })()`);
  info(`[cdp-2fa] Post-login Skip/Cancel clicked: ${skipped}`);
  await sleep(3000);

  // Navigate to myaccount to finalise cookies
  await cdpCall('Page.navigate', { url: 'https://myaccount.google.com/' });
  await sleep(4000);

  // Extract cookies via CDP — use all available fields for v2 format
  info('[cdp-2fa] Extracting final cookies via CDP Network.getAllCookies...');
  const cookieMsg = await cdpCall('Network.getAllCookies');
  const rawCookies = cookieMsg?.result?.cookies || [];

  if (rawCookies.length > 0) {
    // Map ALL CDP fields — preserve everything for accurate v2 restore
    const cookies = rawCookies.map(c => {
      // CDP (Chrome 119+) returns partitionKey as object {topLevelSite, hasCrossSiteAncestor}.
      // Playwright requires partitionKey to be a string or absent.
      const pk = c.partitionKey;
      const partitionKeyStr = typeof pk === 'string' ? pk
        : typeof pk === 'object' && pk !== null ? (pk.topLevelSite ?? undefined)
        : undefined;
      return {
        name: c.name, value: c.value, domain: c.domain, path: c.path ?? '/',
        expires: c.expires || -1,
        httpOnly: c.httpOnly ?? false,
        secure: c.secure ?? false,
        sameSite: c.sameSite ?? 'None',
        ...(partitionKeyStr  ? { partitionKey: partitionKeyStr } : {}),
        ...(c.sourceScheme   ? { sourceScheme: c.sourceScheme }  : {}),
        ...(c.sourcePort     ? { sourcePort: c.sourcePort }      : {}),
        ...(c.priority       ? { priority: c.priority }          : {}),
      };
    });

    // Extract localStorage from key Google origins via CDP Runtime.evaluate
    const origins = [];
    for (const lsOrigin of [
      'https://accounts.google.com',
      'https://myaccount.google.com',
      'https://www.google.com',
    ]) {
      try {
        await cdpCall('Page.navigate', { url: lsOrigin });
        await sleep(2000);
        const lsResult = await cdpCall('Runtime.evaluate', {
          expression: `JSON.stringify((() => {
            const items = [];
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i);
              items.push({ name: k, value: localStorage.getItem(k) });
            }
            return items;
          })())`,
          returnByValue: true,
        });
        const lsItems = JSON.parse(lsResult?.result?.result?.value ?? '[]');
        if (lsItems.length > 0) {
          origins.push({ origin: lsOrigin, localStorage: lsItems });
          info(`[cdp-2fa] localStorage: ${lsOrigin} → ${lsItems.length} entries`);
        }
      } catch (lsErr) {
        info(`[cdp-2fa] localStorage capture failed for ${lsOrigin}: ${lsErr.message}`);
      }
    }

    // Write v2 session format
    const v2State = {
      _version: 2,
      _captured_at: new Date().toISOString(),
      _capture_method: 'cdp_stealth_sidecar',
      _domains: [...new Set(cookies.map(c => c.domain))],
      cookies,
      origins,
    };

    // Merge-save the new Google cookies back into the shared state.json.
    // Using saveStorageState() (not writeFileSync) ensures that other domain cookies
    // already stored in the file (Tailscale, v0, GitHub, etc.) are PRESERVED.
    // saveStorageState() performs TTL-aware cookie merging — longer-TTL wins.
    if (sessionPath) {
      try {
        const { basename: _bn } = await import('node:path');
        // sessionPath = /…/sessions/PRFL-006_samnurnihartalukdar.json
        // Strip the PRFL-NNN_ prefix to recover the bare slug (e.g. "samnurnihartalukdar")
        // so _canonicalBase() in session-manager doesn't assign a new serial and produce
        // a double-PRFL name like PRFL-056_prfl_006_samnurnihartalukdar.
        const _base = _bn(sessionPath, '.json');                   // PRFL-006_samnurnihartalukdar
        const _slug = _base.replace(/^PRFL-\d+_/i, '');           // samnurnihartalukdar
        const { saveStorageState } = await import(`file://${_xiobr}/src/core/session-manager.mjs`);
        await saveStorageState(_slug, v2State);
        info(`[cdp-2fa] Merge-saved ${cookies.length} cookies + ${origins.length} localStorage origins (session=${_base}) (v2)`);
      } catch (_saveErr) {
        // Fallback: direct write if session-manager import fails (e.g. path not available)
        info(`[cdp-2fa] ⚠️ saveStorageState failed (${_saveErr.message?.slice(0, 80)}) — falling back to direct write`);
        const { writeFileSync: _wf, mkdirSync: _mkd } = await import('node:fs');
        const { dirname: _dn } = await import('node:path');
        _mkd(_dn(sessionPath), { recursive: true });
        _wf(sessionPath, JSON.stringify(v2State, null, 2));
        info(`[cdp-2fa] Fallback: wrote ${cookies.length} cookies to ${sessionPath} directly`);
      }
    }


  } else {
    info('[cdp-2fa] ⚠️ No cookies returned by CDP!');
  }

  info('[cdp-2fa] ✅ CDP handler complete');
}
