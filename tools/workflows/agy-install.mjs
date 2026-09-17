/**
 * agy-install.mjs — AGY CLI Install + Auth + R2 Persistence Workflow
 * ────────────────────────────────────────────────────────────────────
 *
 * Installs the Antigravity CLI on a Colab runtime and authenticates it
 * using a persisted Google session from the XIO mesh. Stores the credential
 * encrypted to R2 for reuse across runtimes without re-authenticating.
 *
 * Key facts (researched):
 *   - AGY binary installed to: ~/.local/bin/agy
 *   - OAuth token stored at:   ~/.gemini/jetski-standalone-oauth-token
 *   - Account info stored at:  ~/.gemini/google_accounts.json
 *   - Config dir:              ~/.gemini/antigravity-cli/
 *
 * Flow:
 *   Phase 0: restore_from_r2   — Try to pull existing credential from R2;
 *                                 if valid, skip Phases 1-8.
 *   Phase 1: verify_session    — Check chosen Google session is active in browser.
 *   Phase 2: install_agy       — Run the official install script.
 *   Phase 3: start_agy_auth    — Spawn `agy` in PTY, select "Google OAuth",
 *                                 capture the OAuth URL.
 *   Phase 4: open_oauth_url    — Navigate to the OAuth URL in the browser.
 *   Phase 5: select_account    — Click the target account in the account chooser.
 *   Phase 6: confirm_signin    — Click "Sign in" on the security screen.
 *   Phase 7: copy_auth_code    — Extract the auth code from the success page.
 *   Phase 8: paste_auth_code   — Feed the code to the waiting PTY.
 *   Phase 9: verify_auth       — Confirm authentication succeeded.
 *   Phase 10: persist_to_r2    — Encrypt and upload credential bundle to R2.
 *
 * Params:
 *   session_id   - Gmail account whose browser profile is used for OAuth (required)
 *   r2_key       - R2 object key prefix. Default: "agy-credentials/<session_id>"
 *   force        - Re-authenticate even if R2 credential is valid (default: false)
 *   binary_only  - Only restore/install the binary, skip auth (default: false)
 *
 * R2 Layout (bucket: xio-mesh):
 *   agy-credentials/<session_id>/credential.tar.gz.enc  — encrypted token bundle
 *   agy-credentials/<session_id>/binary                  — the agy binary
 *
 * Encryption:
 *   AES-256-CBC via openssl. Key from env var XIO_AGY_ENCRYPT_KEY (falls back
 *   to a default key — set this env var in production for real security).
 */

export const meta = {
  name:        'agy-install',
  description: 'Install AGY CLI on the Colab runtime, authenticate via Google OAuth using a persisted XIO browser session, and persist the credential encrypted to R2 for reuse across runtimes.',
  requires:    [],
  params: {
    session_id:  'Gmail account whose stored browser cookies are used for OAuth consent (required)',
    r2_key:      '(optional) R2 object key prefix. Default: agy-credentials/<session_id>',
    force:       '(optional) true = ignore R2 cache and re-authenticate. Default: false',
    binary_only: '(optional) true = only install/restore binary, skip auth. Default: false',
  },
};

import path from 'node:path';
import { mkdirSync, existsSync, readFileSync, writeFileSync, unlinkSync, chmodSync } from 'node:fs';
import { execSync, spawn }     from 'node:child_process';
import { createShot }          from '../src/core/wf-shot.mjs';
import { attachTestRunner }    from './_test-runner.mjs';
import { getSession }          from '../src/core/db.mjs';

// ── Constants ──────────────────────────────────────────────────────────────────
const AGY_INSTALL_URL  = 'https://antigravity.google/cli/install.sh';
const HOME             = process.env.HOME ?? '/root';
const AGY_BIN          = `${HOME}/.local/bin/agy`;
const AGY_GEMINI_DIR   = `${HOME}/.gemini`;
const AGY_TOKEN_FILE   = `${AGY_GEMINI_DIR}/jetski-standalone-oauth-token`;
const AGY_ACCOUNTS_FILE = `${AGY_GEMINI_DIR}/google_accounts.json`;
const R2_BUCKET        = 'xio-mesh';
const ENCRYPT_KEY_ENV  = 'XIO_AGY_ENCRYPT_KEY';
const DEFAULT_ENC_KEY  = 'xio-agy-default-key-32charslong!!';
const AGY_CMD_ENV      = () => ({
  ...process.env,
  PATH:    `${HOME}/.local/bin:${process.env.PATH ?? '/usr/local/bin:/usr/bin:/bin'}`,
  TERM:    'xterm-256color',
  HOME,
  // CRITICAL: unset DISPLAY so agy does NOT detect our patchright Chrome (port 9222)
  // and enter "local chrome mode" — which conflicts with our PTY code-paste flow.
  // Without DISPLAY, agy uses pure manual input: we paste the code via PTY → agy
  // exchanges it with Google's token endpoint directly (no browser involvement).
  DISPLAY: '',
});

// ── Load R2 credentials from D1 node_secrets (CF_R2_ENDPOINT not always in env) ──
let _r2Creds = null;
async function loadR2Creds() {
  if (_r2Creds) return _r2Creds;

  // Try env vars first (set on some nodes)
  if (process.env.CF_R2_ENDPOINT && process.env.CF_R2_ACCESS_KEY) {
    _r2Creds = {
      endpoint:  process.env.CF_R2_ENDPOINT,
      accessKey: process.env.CF_R2_ACCESS_KEY,
      secretKey: process.env.CF_R2_SECRET_KEY ?? '',
    };
    return _r2Creds;
  }

  // Fall back to D1 node_secrets via CF API
  const acct  = process.env.CF_ACCOUNT_ID      ?? '';
  const dbId  = process.env.CF_D1_DATABASE_ID  ?? '';
  const token = process.env.CF_API_TOKEN        ?? '';
  if (!acct || !dbId || !token) throw new Error('[r2] Missing CF_ACCOUNT_ID/CF_D1_DATABASE_ID/CF_API_TOKEN');

  // Write query script to a temp file — avoids execSync -c multiline escaping issues
  const pyFile = `/tmp/_r2creds_${Date.now()}.py`;
  writeFileSync(pyFile, [
    'import os, json, urllib.request',
    `acct  = os.environ['CF_ACCOUNT_ID']`,
    `db_id = os.environ['CF_D1_DATABASE_ID']`,
    `tok   = os.environ['CF_API_TOKEN']`,
    `url   = f'https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db_id}/query'`,
    `sql   = "SELECT key, value FROM node_secrets WHERE key IN ('r2_endpoint','r2_access_key_id','r2_secret_access_key')"`,
    `body  = json.dumps({'sql': sql}).encode()`,
    `req   = urllib.request.Request(url, body, {'Content-Type':'application/json','Authorization':f'Bearer {tok}'})`,
    `resp  = urllib.request.urlopen(req, timeout=10).read()`,
    `rows  = json.loads(resp)['result'][0]['results']`,
    `print(json.dumps({r['key']: r['value'] for r in rows}))`,
  ].join('\n'));

  let out;
  try {
    out = execSync(`python3 ${JSON.stringify(pyFile)}`, {
      env: process.env, encoding: 'utf8', timeout: 10_000,
    }).trim();
  } catch (e) {
    try { unlinkSync(pyFile); } catch {}
    throw new Error(`[r2] Failed to load R2 creds from D1: ${e.message?.slice(0, 100)}`);
  } finally {
    try { unlinkSync(pyFile); } catch {}
  }

  const secrets = JSON.parse(out);
  _r2Creds = {
    endpoint:  secrets['r2_endpoint']          ?? `https://${acct}.r2.cloudflarestorage.com`,
    accessKey: secrets['r2_access_key_id']     ?? '',
    secretKey: secrets['r2_secret_access_key'] ?? '',
  };
  return _r2Creds;
}


// ── R2 helpers ─────────────────────────────────────────────────────────────────
async function r2Op(op, key, localPath, logFn) {
  const creds = await loadR2Creds();

  const r2Env = {
    ...process.env,
    _R2_EP: creds.endpoint,
    _R2_AK: creds.accessKey,
    _R2_SK: creds.secretKey,
  };

  // Write Python script to a temp file — avoids execSync -c multiline escaping issues
  const pyFile = `/tmp/_r2op_${op}_${Date.now()}.py`;
  const args   = op === 'put'
    ? [localPath, R2_BUCKET, key]
    : [R2_BUCKET, key, localPath];

  if (op === 'put') {
    writeFileSync(pyFile, [
      'import sys, os, boto3',
      `s3 = boto3.client('s3', endpoint_url=os.environ['_R2_EP'], aws_access_key_id=os.environ['_R2_AK'], aws_secret_access_key=os.environ['_R2_SK'], region_name='auto')`,
      `s3.upload_file(sys.argv[1], sys.argv[2], sys.argv[3])`,
      `print('OK')`,
    ].join('\n'));
  } else {
    writeFileSync(pyFile, [
      'import sys, os, boto3, botocore.exceptions',
      `s3 = boto3.client('s3', endpoint_url=os.environ['_R2_EP'], aws_access_key_id=os.environ['_R2_AK'], aws_secret_access_key=os.environ['_R2_SK'], region_name='auto')`,
      'try:',
      `    s3.download_file(sys.argv[1], sys.argv[2], sys.argv[3])`,
      `    print('OK')`,
      'except botocore.exceptions.ClientError as e:',
      `    code = e.response.get('Error', {}).get('Code', '')`,
      `    print('NOT_FOUND' if 'NoSuchKey' in str(e) or code in ('404', 'NoSuchKey') else 'ERROR:' + str(e))`,
    ].join('\n'));
  }

  let result;
  try {
    result = execSync(
      `python3 ${JSON.stringify(pyFile)} ${args.map(a => JSON.stringify(a)).join(' ')}`,
      { env: r2Env, encoding: 'utf8', timeout: 12_000 }  // 12s max — don't block event loop
    ).trim();
  } catch (e) {
    // Timeout or network error — treat as NOT_FOUND so the workflow continues gracefully
    logFn(`[r2-${op}] ${key}: EXEC_ERROR ${e.message?.slice(0, 100)}`);
    return false;
  } finally {
    try { unlinkSync(pyFile); } catch {}
  }

  logFn(`[r2-${op}] ${key}: ${result}`);
  return result === 'OK';
}



// ── Encryption helpers (openssl AES-256-CBC PBKDF2) ───────────────────────────
function cryptFile(mode, src, dst) {
  const key = process.env[ENCRYPT_KEY_ENV] ?? DEFAULT_ENC_KEY;
  const flag = mode === 'enc' ? '' : '-d';
  execSync(`openssl enc -aes-256-cbc -pbkdf2 ${flag} -k ${JSON.stringify(key)} -in ${JSON.stringify(src)} -out ${JSON.stringify(dst)}`, { stdio: 'pipe' });
}

// ── Check if AGY is authenticated ─────────────────────────────────────────────
// agy v1.1.19 uses composite_token_storage (file-based, no D-Bus)
import { readdirSync } from 'node:fs';
const AGY_STATE_DIR = `${AGY_GEMINI_DIR}/antigravity-cli`;
function isAgyAuthed() {
  try {
    if (!existsSync(AGY_BIN)) return false;

    // Check composite_token_storage dir (agy v1.1.19)
    if (existsSync(AGY_STATE_DIR)) {
      const files = (() => { try { return readdirSync(AGY_STATE_DIR); } catch { return []; } })();
      for (const f of files) {
        if (/token|credential|oauth|auth/i.test(f) && !f.includes('.log') && !f.endsWith('.pbtxt')) {
          try {
            const raw = readFileSync(`${AGY_STATE_DIR}/${f}`, 'utf8');
            if (raw.includes('refresh_token') || raw.includes('access_token')) return true;
          } catch {}
        }
      }
    }

    // Fallback: legacy token path (agy <1.1.19)
    if (existsSync(AGY_TOKEN_FILE)) {
      const t = JSON.parse(readFileSync(AGY_TOKEN_FILE, 'utf8'));
      return !!(t?.token?.refresh_token ?? t?.refresh_token);
    }

    return false;
  } catch { return false; }
}

// ── Main workflow ──────────────────────────────────────────────────────────────
export async function run(ctx, params = {}) {
  attachTestRunner(ctx, import.meta.url);

  const sessionId  = params?.session_id ?? ctx.sessionId;
  const forceAuth  = params?.force       === true || params?.force       === 'true';
  const binaryOnly = params?.binary_only === true || params?.binary_only === 'true';
  if (!sessionId) throw new Error('[agy-install] session_id param is required');

  const session = getSession(sessionId);
  // Local SQLite sessions table uses 'display_name' (format: email) not 'account_email'
  // Gracefully derive email — never throw if session is missing, since the browser
  // profile may still be valid even if the local DB hasn't synced yet.
  const email = (
    session?.display_name?.includes('@') ? session.display_name :
    session?.account_email?.includes('@') ? session.account_email :
    `${sessionId}@gmail.com`
  );


  const r2Prefix  = params?.r2_key ?? `agy-credentials/${sessionId}`;
  const r2CredKey = `${r2Prefix}/credential.tar.gz.enc`;
  const r2BinKey  = `${r2Prefix}/binary`;

  const _jobDir = ctx.jobDir ?? (ctx.dirName ? `/content/xio-mesh/jobs/${ctx.dirName}` : `/tmp/agy-install-${Date.now()}`);
  const _evDir  = `${_jobDir}/steps`;
  mkdirSync(_evDir, { recursive: true });
  const shot = createShot(_evDir, { logFn: ctx.log.bind(ctx) });
  shot.setPage(ctx.page);

  ctx.log(`[agy-install] session=${sessionId} email=${email}`);
  ctx.log(`[agy-install] r2_prefix=${r2Prefix} force=${forceAuth} binary_only=${binaryOnly}`);

  // Shared state across steps
  let oauthUrl      = null;
  let authCode      = null;
  let credRestored  = false;
  let ptyOutputFile = null;
  let ptyProcess    = null;
  // NOTE: We use agy's NATIVE token exchange (via PTY codeSignal file).
  // agy handles PKCE internally and registers the session with the Antigravity backend.
  // Direct PKCE bypass was tried but agy said "not logged into Antigravity" because
  // the Google OAuth token alone is insufficient — agy also registers with its backend.

  // ──────────────────────────────────────────────────────────────────────────
  // Phase 0: Restore credential from R2
  // ──────────────────────────────────────────────────────────────────────────
  await ctx.step('restore_from_r2', async () => {
    if (forceAuth) { ctx.log('[agy-install] force=true — skipping R2 restore'); return; }

    // 0a: Binary restore — SKIP if already installed (saves 12s of R2 timeout)
    if (existsSync(AGY_BIN)) {
      ctx.log('[agy-install] Binary already installed — skipping R2 binary check');
    } else {

      ctx.log('[agy-install] Binary absent — checking R2...');
      const tmp = `/tmp/agy_bin_${Date.now()}`;
      const ok  = await r2Op('get', r2BinKey, tmp, ctx.log.bind(ctx));
      if (ok) {
        mkdirSync(path.dirname(AGY_BIN), { recursive: true });
        execSync(`cp ${JSON.stringify(tmp)} ${JSON.stringify(AGY_BIN)}`, { stdio: 'pipe' });
        chmodSync(AGY_BIN, 0o755);
        try { unlinkSync(tmp); } catch {}
        ctx.log('[agy-install] ✅ Binary restored from R2');
      }
    }

    if (binaryOnly) return;

    // 0b: Skip cred R2 check if already authenticated (saves 12s block)
    if (isAgyAuthed()) {
      // Quick functional probe: does agy ACTUALLY respond? A PKCE token has the right
      // keys but is missing Antigravity backend registration → agy returns "authentication required".
      // We cannot distinguish valid from invalid by file content alone.
      let agyReallyWorks = false;
      try {
        const { execSync: esTmp } = await import('child_process');
        const probeOut = esTmp(
          `script -q -c 'echo test | timeout 20 ${JSON.stringify(AGY_BIN)}' /dev/null 2>&1 || true`,
          { env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 25_000, shell: true }
        );
        agyReallyWorks = !probeOut.toLowerCase().includes('authentication required') &&
                         !probeOut.toLowerCase().includes('not signed in') &&
                         !probeOut.toLowerCase().includes('not logged') &&
                         !probeOut.toLowerCase().includes('you are not') &&
                         probeOut.trim().length > 3;
        if (!agyReallyWorks) {
          ctx.log(`[agy-install] ⚠️ Token file exists but agy not working (${probeOut.slice(0, 120).trim()}) — clearing stale token, will re-authenticate`);
          try { unlinkSync(AGY_TOKEN_FILE); } catch {}
        }
      } catch (e) {
        ctx.log(`[agy-install] agy probe error: ${e.message.slice(0, 80)} — will re-authenticate`);
      }
      if (agyReallyWorks) {
        ctx.log('[agy-install] Already authenticated and working — skipping R2 cred restore');
        credRestored = true;
        return;
      }
      // Fall through: run full auth flow
      return;
    }

    // 0c: Restore credential bundle from R2
    ctx.log('[agy-install] Checking R2 for credential...');
    const encTmp = `/tmp/agy_enc_${Date.now()}`;
    const tarTmp = `/tmp/agy_tar_${Date.now()}.tar.gz`;
    const ok = await r2Op('get', r2CredKey, encTmp, ctx.log.bind(ctx));
    if (!ok) { ctx.log('[agy-install] No R2 credential — will authenticate fresh'); return; }

    try {
      cryptFile('dec', encTmp, tarTmp);
      execSync(`tar -xzf ${JSON.stringify(tarTmp)} -C ${JSON.stringify(HOME)}`, { stdio: 'pipe' });
      ctx.log('[agy-install] ✅ Credential bundle extracted');
    } catch (e) {
      ctx.log(`[agy-install] ⚠️ Decrypt/extract failed: ${e.message} — will re-authenticate`);
      return;
    } finally {
      try { unlinkSync(encTmp); } catch {}
      try { unlinkSync(tarTmp); } catch {}
    }

    if (isAgyAuthed()) {
      ctx.log('[agy-install] ✅ Restored credential is valid — skipping auth flow');
      credRestored = true;
    } else {
      ctx.log('[agy-install] ⚠️ Restored credential is expired — will re-authenticate');
    }
  });

  // ──────────────────────────────────────────────────────────────────────────
  // Phase 1: Ensure Google session (inline — no external workflow needed)
  // Pattern mirrors self-spawn: ensureSessionState pull → live verify →
  // inline google-signin sidecar if still invalid → re-verify → throw only
  // if all attempts exhausted.
  // ──────────────────────────────────────────────────────────────────────────
  await ctx.step('verify_session', async () => {
    if (credRestored && !forceAuth) { ctx.log('[agy-install] Skipping session check (credential restored)'); return; }
    if (binaryOnly)                 { ctx.log('[agy-install] Skipping session check (binary_only)'); return; }

    const { verifyGoogleSession }  = await import('../src/core/session-verifier.mjs');
    const { ensureSessionState }   = await import('../src/core/session-manager.mjs');

    // ── Step 1: Pull session from D1/R2 if not on disk (fresh boot) ──────────
    ctx.log(`[agy-install] Pulling session state for ${sessionId} (on-demand)...`);
    await ensureSessionState(sessionId).catch(e =>
      ctx.log(`[agy-install] ensureSessionState warning (non-fatal): ${e.message}`)
    );

    // ── Step 2: Live browser check ────────────────────────────────────────────
    await ctx.page.goto('https://myaccount.google.com/', {
      waitUntil: 'domcontentloaded', timeout: 15000,
    }).catch(() => {});
    await ctx.page.waitForTimeout(1500);

    let isValid = await verifyGoogleSession(ctx.page, email);
    await shot(isValid ? 'session_valid' : 'session_invalid');

    if (isValid) {
      ctx.log('[agy-install] ✅ Google session valid');
      return;
    }

    // ── Step 3: Session invalid — run inline google-signin (like self-spawn) ──
    ctx.log(`[agy-install] ⚠️ Session invalid — running inline google-signin for ${sessionId}...`);
    try {
      const result = await ctx.runInline('google-signin', sessionId, {});
      ctx.log(`[agy-install] Inline google-signin result: ${JSON.stringify(result ?? {}).slice(0,120)}`);
    } catch (e) {
      ctx.log(`[agy-install] ⚠️ Inline google-signin threw: ${e.message}`);
    }

    // ── Step 4: Re-verify after inline signin ────────────────────────────────
    await ctx.page.goto('https://myaccount.google.com/', {
      waitUntil: 'domcontentloaded', timeout: 15000,
    }).catch(() => {});
    await ctx.page.waitForTimeout(2000);
    isValid = await verifyGoogleSession(ctx.page, email);
    await shot(isValid ? 'session_valid_after_signin' : 'session_invalid_after_signin');

    if (!isValid) throw new Error(`[agy-install] Google session for ${email} still invalid after inline signin attempt.`);
    ctx.log('[agy-install] ✅ Google session valid after inline signin');
  });


  // ──────────────────────────────────────────────────────────────────────────
  // Phase 2: Install AGY CLI
  // ──────────────────────────────────────────────────────────────────────────
  await ctx.step('install_agy', async () => {
    if (existsSync(AGY_BIN)) {
      try {
        const ver = execSync(`${JSON.stringify(AGY_BIN)} --version`, { env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 10_000 }).trim();
        ctx.log(`[agy-install] Binary present: ${ver}`);
      } catch { ctx.log('[agy-install] Binary present but version check failed'); }
      return;
    }

    ctx.log('[agy-install] Running official install script...');
    mkdirSync(path.dirname(AGY_BIN), { recursive: true });

    try {
      const out = execSync(`curl -fsSL ${AGY_INSTALL_URL} | bash`, {
        shell: '/bin/bash', env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 120_000,
      });
      ctx.log(`[agy-install] Install output: ${out.slice(0, 400)}`);
    } catch (e) {
      ctx.log(`[agy-install] Install stderr: ${(e.stderr ?? '').slice(0, 300)}`);
    }

    if (!existsSync(AGY_BIN)) throw new Error(`AGY binary not found at ${AGY_BIN} after install`);
    const ver = execSync(`${JSON.stringify(AGY_BIN)} --version`, { env: AGY_CMD_ENV(), encoding: 'utf8' }).trim();
    ctx.log(`[agy-install] ✅ Installed: ${ver}`);
  });

  // ──────────────────────────────────────────────────────────────────────────
  // Auth phases — skipped if credential was restored from R2
  // ──────────────────────────────────────────────────────────────────────────
  if (!credRestored || forceAuth) {

    // ────────────────────────────────────────────────────────────────────────
    // Phase 3: Launch AGY PTY via xb_shell — zero event-loop impact
    // The PTY Python script runs in a completely separate process tree.
    // Node.js only polls signal files via setInterval — no spawn, no pipes.
    // ────────────────────────────────────────────────────────────────────────
    await ctx.step('start_agy_auth', async () => {
      ctx.log('[agy-install] Launching AGY CLI via xb_shell PTY...');

      const ts        = Date.now();
      const urlSignal = `/tmp/agy_url_${ts}`;
      const codeSignal= `/tmp/agy_code_${ts}`;
      const logFile   = `/tmp/agy_pty_${ts}.log`;
      ptyOutputFile   = `/tmp/agy_pty_${ts}`;

      // Write PTY controller script -- corrected screen sequence:
      //   SCR0: Login method -> Enter        (option 1 pre-highlighted)
      //   SCR1: Terms of Service -> Tab+Enter  ([Done] focused)
      //   SCR2: Trust folder -> Enter          (Yes pre-selected)
      //   SCR3: OAuth URL + code prompt        -> capture URL
      const pyLines = [
        'import pty, os, sys, time, re, threading, subprocess, fcntl, struct, termios',
        `_log = open(${JSON.stringify(logFile)}, "w", buffering=1)`,
        'sys.stdout = _log; sys.stderr = _log',
        // DISPLAY='' retained; primary Chrome block is SIGSTOP
        `env = {**os.environ, "TERM":"xterm-256color", "COLUMNS":"220", "LINES":"50", "PATH":"${HOME}/.local/bin:"+os.environ.get("PATH","/usr/bin"), "HOME":"${HOME}", "DISPLAY":""}`,
        `master_fd, slave_fd = pty.openpty()`,
        `fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))`,
        `fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))`,
        `proc = subprocess.Popen(["${AGY_BIN}"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env)`,
        `os.close(slave_fd)`,
        `buf = b""; lock = threading.Lock()`,
        `responded = set()`,
        `def rd():`,
        `    global buf`,
        `    while True:`,
        `        try:`,
        `            d = os.read(master_fd, 4096)`,
        `            if not d: break`,
        `            with lock: buf += d`,
        `            sys.stdout.write(d.decode("utf-8",errors="replace")); sys.stdout.flush()`,
        `            resp = b""`,
        `            if b"\\x1b[?2026$p" in d and "2026" not in responded:`,
        `                resp += b"\\x1b[?2026;2$y"; responded.add("2026")`,
        `            if b"\\x1b[?2027$p" in d and "2027" not in responded:`,
        `                resp += b"\\x1b[?2027;2$y"; responded.add("2027")`,
        `            if b"\\x1b[?u" in d and "kitty" not in responded:`,
        `                resp += b"\\x1b[?1u"; responded.add("kitty")`,
        `            if resp:`,
        `                time.sleep(0.05); os.write(master_fd, resp)`,
        `                print("TERM_RESP:"+repr(resp)); sys.stdout.flush()`,
        `        except OSError: break`,
        `threading.Thread(target=rd, daemon=True).start()`,
        ``,
        `def strip_ansi(s): return re.sub(r"\\x1b\\[[\\d;?=><]*(?:\\$[a-zA-Z]|[a-zA-Z])|\\x1b[=>M]","",s)`,
        `def txt():`,
        `    with lock: return strip_ansi(buf.decode("utf-8",errors="replace"))`,
        `def wait(pat, secs=25):`,
        `    dl = time.time()+secs`,
        `    while time.time()<dl:`,
        `        if re.search(pat, txt(), re.I|re.S): return True`,
        `        time.sleep(0.25)`,
        `    return False`,
        `def send(b): os.write(master_fd, b); time.sleep(0.4)`,
        `url_pat = r'https://accounts\\.google\\.com/o/oauth2[^\\s\\x00-\\x1f]+'`,
        `print("BOOT 50x220 PTY"); sys.stdout.flush()`,
        `time.sleep(3)`,
        `t0 = txt(); print("TXT_3s:"+repr(t0[:500])); sys.stdout.flush()`,
        `m0 = re.search(url_pat, t0)`,
        `if m0:`,
        `    open(${JSON.stringify(urlSignal)},"w").write(m0.group(0).strip())`,
        `    print("URL_DIRECT_IMMEDIATE:"+m0.group(0)[:80]); sys.stdout.flush()`,
        `else:`,
        `    # SCR0: Login method -- "> 1. Google OAuth" pre-highlighted`,
        `    if wait("Select login method|Google OAuth", 25):`
        `        print("SCR0_LOGIN_SELECT"); sys.stdout.flush()`
        `        send(b"\\r")`
        `        print("SCR0_ENTER_SENT"); sys.stdout.flush()`
        `    else: print("SCR0_SKIP:"+repr(txt()[:300])); sys.stdout.flush()`,
        `    # SCR1: Terms of Service -- "[Previous] > Done" -- Tab+Enter`,
        `    if wait("Terms of Service|Data Use|agree|Privacy Policy", 25):`,
        `        print("SCR1_TOS"); sys.stdout.flush()`,
        `        send(b"\\t")`,
        `        time.sleep(0.3)`,
        `        send(b"\\r")`,
        `        print("SCR1_TOS_DONE"); sys.stdout.flush()`,
        `    else: print("SCR1_TOS_SKIP:"+repr(txt()[-200:])); sys.stdout.flush()`,
        `    # SCR2: Trust folder -- "> Yes, I trust" pre-selected -- Enter`,
        `    if wait("trust|Do you trust|trust this folder", 20):`,
        `        print("SCR2_TRUST"); sys.stdout.flush()`,
        `        send(b"\\r")`,
        `        print("SCR2_TRUST_DONE"); sys.stdout.flush()`,
        `    else: print("SCR2_TRUST_SKIP"); sys.stdout.flush()`,
        `    # SCR3: OAuth URL + "paste authorization code below" prompt`,
        `    if not wait("accounts.google.com|authorization code|automatically redirected", 45):`,
        `        print("SCR3_URL_TIMEOUT:"+repr(txt()[-500:])); sys.stdout.flush()`,
        `        proc.terminate(); sys.exit(1)`,
        `    time.sleep(0.5)`,
        `    t3 = txt(); print("SCR3:"+repr(t3[-500:])); sys.stdout.flush()`,
        `    m3 = re.search(url_pat, t3)`,
        `    if m3:`,
        `        open(${JSON.stringify(urlSignal)},"w").write(m3.group(0).strip())`,
        `        print("URL_WRITTEN:"+m3.group(0)[:80]); sys.stdout.flush()`,
        `    else:`,
        `        print("URL_PARSE_FAIL:"+repr(t3[-400:])); sys.stdout.flush()`,
        `        proc.terminate(); sys.exit(1)`,
        `dl2 = time.time()+720`,
        `while time.time()<dl2:`,
        `    if os.path.exists(${JSON.stringify(codeSignal)}):`,
        `        code = open(${JSON.stringify(codeSignal)}).read().strip()`,
        `        time.sleep(0.5)`,
        `        os.write(master_fd, code.encode() + b"\\r")  # PTY Enter key (CR)`,
        `        t_code_sent = time.time()`,
        `        print("CODE_SENT:"+code[:12]+"... t="+str(int(t_code_sent))); sys.stdout.flush()`,
        `        time.sleep(5)`,
        `        print("CODE_ENTER_SENT"); sys.stdout.flush(); break`,
        `    time.sleep(0.5)`,
        `else: print("CODE_TIMEOUT"); sys.stdout.flush()`,
        `import signal as _sig`,
        `# Watch ALL dirs where agy might write tokens`,
        `_watch_dirs = [os.path.expanduser(d) for d in ['~/.gemini','~/.config','~/.local/share','~/.local/bin','/tmp']]`,
        `_watch_dirs = [d for d in _watch_dirs if os.path.isdir(d)]`,
        `initial_mtimes = {}`,
        `for _wd in _watch_dirs:`,
        `    try:`,
        `        for _r,_d,_fs in os.walk(_wd):`,
        `            for _f in _fs:`,
        `                _fp = os.path.join(_r,_f)`,
        `                try: initial_mtimes[_fp] = os.path.getmtime(_fp)`,
        `                except: pass`,
        `    except: pass`,
        `open('/tmp/agy_auth_baseline','w').write(str(time.time())); os.sync()`,
        `print('WATCHING_FOR_TOKEN files='+str(len(initial_mtimes))+' dirs='+str(len(_watch_dirs))); sys.stdout.flush()`,
        `t_watch = time.time(); auth_ok = False; buf_len_at_code = len(buf)`,
        `def _scan_all():`,
        `    global auth_ok`,
        `    for _wd2 in _watch_dirs:`,
        `        try:`,
        `            for _r,_d,_fs in os.walk(_wd2):`,
        `                for _f in _fs:`,
        `                    _fp2 = os.path.join(_r,_f)`,
        `                    try:`,
        `                        _mt2 = os.path.getmtime(_fp2)`,
        `                        if _fp2 not in initial_mtimes or _mt2 > initial_mtimes.get(_fp2,0)+0.5:`,
        `                            _raw2 = open(_fp2,'r',errors='ignore').read(4096)`,
        `                            if 'refresh_token' in _raw2 or 'access_token' in _raw2 or 'jetski' in _fp2.lower():`,
        `                                print('TOKEN_WRITTEN:'+_fp2+' size='+str(len(_raw2))); sys.stdout.flush()`,
        `                                auth_ok = True`,
        `                    except: pass`,
        `        except: pass`,
        `    return auth_ok`,
        `def _fsdiff():`,
        `    import subprocess as _sp2`,
        `    try:`,
        `        _out2 = _sp2.check_output(['find','/root','-newer','/tmp/agy_auth_baseline','-type','f'],text=True,stderr=_sp2.DEVNULL,timeout=8).strip()`,
        `        print('NEW_FILES:'+repr(_out2[:800])); sys.stdout.flush()`,
        `    except Exception as _e2: print('FSDIFF_ERR:'+str(_e2)); sys.stdout.flush()`,
        `while time.time() - t_watch < 240:`,
        `    if _scan_all(): break`,
        `    with lock: _new_bytes = buf[buf_len_at_code:].decode('utf-8',errors='replace')`,
        `    if len(_new_bytes) > 1500 and 'authorization code' not in _new_bytes.lower() and not auth_ok:`,
        `        print('AUTH_SCREEN_GONE new_bytes='+str(len(_new_bytes))); sys.stdout.flush()`,
        `        _fsdiff()`,
        `        _t_gone = time.time()`,
        `        while time.time() - _t_gone < 120:`,
        `            time.sleep(1)`,
        `            if _scan_all(): break`,
        `        if not auth_ok:`,
        `            print('AUTH_SCREEN_GONE_NO_TOKEN'); sys.stdout.flush()`,
        `            _fsdiff()`,
        `            buf_len_at_code = len(buf)`,
        `        else: break`,
        `    time.sleep(1)`,
        `if not auth_ok: print('AUTH_WATCH_TIMEOUT'); sys.stdout.flush()`,
        `time.sleep(1)`,
        `try: proc.send_signal(_sig.SIGTERM); proc.wait(timeout=10)`,
        `except subprocess.TimeoutExpired: proc.terminate()`,
        `print('AGY_SHUTDOWN_OK'); sys.stdout.flush()`,
        `print('DONE'); sys.stdout.flush()`,
      ];

      const pyFile = `/tmp/agy_pty_ctrl_${ts}.py`;
      writeFileSync(pyFile, pyLines.join('\n'));

      // SIGSTOP Chrome for agy startup window (~200 ms detection phase).
      // agy is Go -- direct Linux syscalls, LD_PRELOAD cannot intercept Go net stack.
      // Chrome stopped -> CDP probe gets no response -> agy falls through to PTY mode.
      // SIGCONT after 3 s (safe: Playwright CDP keepalive timeout is >= 30 s).
      let stoppedChromePid = null;
      try {
        const { execSync: esStop } = await import('child_process');
        const pidStr = esStop(
          'lsof -ti tcp:9222 2>/dev/null | head -1',
          { encoding: 'utf8', shell: true }
        ).trim();
        if (pidStr) {
          stoppedChromePid = pidStr;
          esStop(`kill -STOP ${pidStr} 2>/dev/null || true`, { shell: true, timeout: 2000 });
          ctx.log(`[agy-install] Chrome PID ${pidStr} paused (SIGSTOP) for agy startup window`);
        } else {
          ctx.log('[agy-install] No Chrome on port 9222 -- no SIGSTOP needed');
        }
      } catch (e) {
        ctx.log(`[agy-install] SIGSTOP attempt (non-fatal): ${e.message?.slice(0, 80)}`);
      }

      // Launch PTY while Chrome is stopped
      const shellCmd = `nohup python3 -u ${JSON.stringify(pyFile)} >/dev/null 2>&1 &`;
      const { execSync: es } = await import('child_process');
      es(shellCmd, { env: AGY_CMD_ENV(), timeout: 3000 });
      ctx.log(`[agy-install] PTY launched. Log: ${logFile} (tail remotely: xb_shell "tail -f ${logFile}")`);

      // Resume Chrome after 3 s -- agy startup detection done within ~200 ms
      await new Promise(r => setTimeout(r, 3000));
      if (stoppedChromePid) {
        try {
          const { execSync: esCont } = await import('child_process');
          esCont(`kill -CONT ${stoppedChromePid} 2>/dev/null || true`, { shell: true, timeout: 2000 });
          ctx.log(`[agy-install] Chrome PID ${stoppedChromePid} resumed (SIGCONT) -- browser ready for OAuth`);
        } catch (e) {
          ctx.log(`[agy-install] SIGCONT (non-fatal): ${e.message?.slice(0, 80)}`);
        }
      }

      // Real-time PTY log streaming + URL signal poll.
      // Streams new PTY log bytes every 2 s to ctx.log for live remote visibility.
      // Timeout 120 s (covers TOS ~25 s + Trust ~20 s screens before URL appears).
      // For continuous live tail: xb_shell -> `tail -f ${logFile}` on Colab runtime.
      await new Promise((resolve, reject) => {
        let elapsed = 0;
        let lastLogOffset = 0;
        const interval = setInterval(async () => {
          elapsed += 2;
          try {
            const newOutput = es(
              `tail -c +${lastLogOffset + 1} ${JSON.stringify(logFile)} 2>/dev/null | head -c 600`,
              { encoding: 'utf8', timeout: 2000, shell: true }
            ).trim();
            if (newOutput) {
              ctx.log(`[agy-pty t=${elapsed}s] ${newOutput.replace(/\n/g, ' | ').slice(0, 500)}`);
              try {
                const sz = parseInt(
                  es(`wc -c < ${JSON.stringify(logFile)} 2>/dev/null`, { encoding: 'utf8', timeout: 1000, shell: true }).trim()
                );
                if (!isNaN(sz)) lastLogOffset = sz;
              } catch {}
            }
          } catch {}
          if (existsSync(urlSignal)) {
            oauthUrl = readFileSync(urlSignal, 'utf8').trim();
            clearInterval(interval);
            clearTimeout(tmout);
            resolve();
          }
        }, 2000);
        const tmout = setTimeout(() => {
          clearInterval(interval);
          try {
            const tail = es(`tail -30 ${JSON.stringify(logFile)} 2>/dev/null`, { encoding: 'utf8', timeout: 2000 }).trim();
            ctx.log(`[agy-pty] TIMEOUT (120s). Final log:\n${tail}`);
          } catch {}
          reject(new Error('Timeout waiting for OAuth URL (120s)'));
        }, 120_000);
      });

      ctx.log(`[agy-install] OAuth URL captured: ${oauthUrl.slice(0, 80)}...`);
      // No consumerOAuth CDP wait -- agy is in PTY mode (no Chrome at startup -> no CDP subscription).
    });
    // ────────────────────────────────────────────────────────────────────────
    // Phase 4: Open OAuth URL in browser
    // ────────────────────────────────────────────────────────────────────────
    await ctx.testStep('open_oauth_url', async () => {
      if (!oauthUrl) throw new Error('No OAuth URL available');

      // Add login_hint to agy's native OAuth URL to bypass Google account chooser.
      // This keeps the original PKCE code_challenge intact (just adds a hint param).
      const urlWithHint = oauthUrl.includes('login_hint') ? oauthUrl
        : `${oauthUrl}&login_hint=${encodeURIComponent(email)}`;
      ctx.log(`[agy-install] Navigating to OAuth URL with login_hint (skip account chooser)...`);
      ctx.log(`[agy-install] URL preview: ${urlWithHint.slice(0, 120)}...`);

      await ctx.page.goto(urlWithHint, { waitUntil: 'commit', timeout: 60_000 })
        .catch(() => ctx.log('[agy-install] goto commit timeout — checking URL'));

      // Wait for redirect chain to settle
      const urlDeadline = Date.now() + 20_000;
      while (Date.now() < urlDeadline) {
        const u = ctx.page.url();
        if (u.includes('accounts.google.com') || u.includes('antigravity.google')) break;
        await ctx.page.waitForTimeout(800);
      }
      const postUrl = ctx.page.url();
      ctx.log(`[agy-install] Post-nav URL: ${postUrl.slice(0, 120)}`);
    }, {
      verify:      async p => p.url().includes('accounts.google.com') || p.url().includes('antigravity.google'),
      verifyLabel: 'Google OAuth page loaded',
    });


    // ────────────────────────────────────────────────────────────────────────
    // Phase 5: Select account
    // ────────────────────────────────────────────────────────────────────────
    await ctx.testStep('select_account', async () => {
      await ctx.page.waitForTimeout(1500);
      const url = ctx.page.url();
      ctx.log(`[agy-install] Current URL: ${url}`);

      if (!url.includes('accounts.google.com')) {
        ctx.log('[agy-install] Not on account chooser — skipping');
        return;
      }
      if (url.includes('antigravity.google') || url.includes('oauth-callback')) {
        ctx.log('[agy-install] Already at callback — skipping');
        return;
      }

      ctx.log(`[agy-install] Looking for account: ${email}`);

      // ROOT CAUSE: locator('text="email"') and getByText() find the nested SPAN inside
      // the LI, not the LI itself. Clicking a span doesn't trigger Google's SPA navigation
      // handler (which is registered on the LI). Solution: always target the LI element.
      //
      // Strategy A: li:has-text() — finds the LI that CONTAINS the email text, clicks LI
      //   This is the most reliable: Playwright sends real mouse events to the LI
      let clickDone = false;

      try {
        const liByText = ctx.page.locator(`li:has-text("${email}")`).first();
        const cnt = await liByText.count().catch(() => 0);
        if (cnt > 0) {
          await liByText.click({ timeout: 6000 });
          ctx.log('[agy-install] ✅ Clicked via li:has-text locator');
          clickDone = true;
        }
      } catch (e) {
        ctx.log(`[agy-install] li:has-text click failed: ${e.message?.slice(0,60)}`);
      }

      // Strategy B: [data-identifier] attribute on LI (Google sometimes adds this)
      if (!clickDone) {
        try {
          const byAttr = ctx.page.locator(`[data-identifier*="${email}"]`).first();
          const cnt = await byAttr.count().catch(() => 0);
          if (cnt > 0) {
            await byAttr.click({ timeout: 5000 });
            ctx.log('[agy-install] ✅ Clicked via data-identifier locator');
            clickDone = true;
          }
        } catch {}
      }

      // Strategy C (universal fallback): get the LI's getBoundingClientRect, use page.mouse.click()
      // Works even when React/SPA prevents programmatic clicks — real OS-level pointer event
      if (!clickDone) {
        const coords = await ctx.page.evaluate((targetEmail) => {
          // Find the outermost clickable LI containing this email
          const rows = [...document.querySelectorAll(
            'li, [role="listitem"], [role="option"], [data-identifier], [data-email]'
          )];
          const row = rows.find(el => el.textContent?.includes(targetEmail));
          if (row) {
            const r = row.getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), via: 'li' };
          }
          // Fallback: shortest element whose full text IS the email (most specific match)
          const all = [...document.querySelectorAll('div, span, a')]
            .filter(e => e.textContent?.trim() === targetEmail)
            .sort((a, b) => a.textContent.length - b.textContent.length);
          if (all.length > 0) {
            const r = all[0].getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), via: 'text' };
          }
          return null;
        }, email);

        if (coords) {
          await ctx.page.mouse.click(coords.x, coords.y);
          ctx.log(`[agy-install] ✅ Mouse click at (${coords.x}, ${coords.y}) via ${coords.via}`);
          clickDone = true;
        } else {
          ctx.log('[agy-install] ⚠️ Account not found in DOM — no coordinates');
        }
      }

      await ctx.page.waitForTimeout(3000);
      ctx.log(`[agy-install] Post-click URL: ${ctx.page.url().slice(0, 80)}`);

    }, {
      verify: async p => {
        const u = p.url();
        // Must have PROGRESSED past the account chooser page
        if (u.includes('antigravity.google') || u.includes('oauth-callback')) return true;
        if (u.includes('accounts.google.com') && !u.includes('accountchooser') && !u.includes('accountchooser')) return true;
        return false;
      },
      verifyLabel: 'Moved past account chooser',
    });

    // ────────────────────────────────────────────────────────────────────────
    // Phase 6: Confirm sign-in
    // ────────────────────────────────────────────────────────────────────────
    await ctx.testStep('confirm_signin', async () => {
      await ctx.page.waitForTimeout(2000);
      ctx.log(`[agy-install] Pre-signin URL: ${ctx.page.url()}`);

      // ── Drive through the full Google OAuth consent chain ────────────────
      // After our PKCE URL, Google may show multiple screens before issuing the code:
      //   1. /firstparty/nativeapp  → "Make sure you downloaded this app" → click Sign in
      //   2. Consent screen         → "Allow Antigravity to access..." → click Allow/Continue
      //   3. antigravity.google/oauth-callback?code=... → done
      //
      // We loop up to 120s, detecting each screen and clicking the right button,
      // until the URL is the callback URL.

      let atCallback = false;
      const deadline = Date.now() + 120_000;

      while (Date.now() < deadline && !atCallback) {
        await ctx.page.waitForTimeout(2000);
        const url = ctx.page.url();
        ctx.log(`[agy-install] consent-loop URL: ${url.slice(0, 100)}`);

        if (url.includes('antigravity.google') || url.includes('oauth-callback')) {
          ctx.log('[agy-install] ✅ At callback URL — consent flow complete');
          atCallback = true; break;
        }

        if (!url.includes('accounts.google.com') && !url.includes('google.com')) {
          ctx.log(`[agy-install] ⚠️ Unexpected URL: ${url.slice(0,80)} — stopping`);
          break;
        }

        // Detect which action button is present (Allow / Continue / Sign in / Next)
        // and click it with a REAL Playwright mouse event (raw btn.click() via evaluate()
        // doesn't trigger Google's SPA event handlers → page never navigates).
        const btnLabel = await ctx.page.evaluate(() => {
          const all = [...document.querySelectorAll('button,[role="button"],input[type="submit"]')];
          // Priority: Allow > Continue > Sign in (avoid Cancel/Deny)
          const priority = [/^allow$/i, /^continue$/i, /sign.?in/i, /^next$/i, /confirm/i];
          for (const re of priority) {
            const btn = all.find(b => re.test((b.textContent || b.value || b.innerText || '').trim()));
            if (btn && !/(cancel|deny|not now|remove)/i.test(btn.textContent || '')) {
              return (btn.textContent || btn.value || '').trim().slice(0, 40);
            }
          }
          return 'none-found:' + all.map(b => (b.textContent || b.value || '').trim().slice(0,20)).join('|');
        });
        ctx.log(`[agy-install] consent-loop detected button: "${btnLabel}"`);

        if (!btnLabel.startsWith('none-found:')) {
          // Use Playwright locator for REAL mouse events — works with Google SPA
          let clickedViaPlaywright = false;
          // ALWAYS use page.mouse.click — locator.click() blocks on navigation
          const coords = await ctx.page.evaluate((label) => {
            const all = [...document.querySelectorAll('button,[role="button"],input[type="submit"]')];
            const btn = all.find(b => {
              const t = (b.textContent || b.value || b.innerText || '').trim().slice(0,40);
              return t === label || new RegExp(label, 'i').test(t);
            });
            if (btn) {
              const r = btn.getBoundingClientRect();
              if (r.width > 0 && r.height > 0)
                return { x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) };
            }
            return null;
          }, btnLabel);
          if (coords) {
            await ctx.page.mouse.click(coords.x, coords.y);
            try {
              const { execSync } = await import('child_process');
              execSync(`DISPLAY=:99 xdotool mousemove ${coords.x} ${coords.y} click 1`, { timeout: 3000 });
            } catch {}
            clickedViaPlaywright = true;
          }
          ctx.log(`[agy-install] consent-loop click result: ${clickedViaPlaywright ? 'clicked:'+btnLabel : 'FAILED'}`);
        }


        // Wait for navigation after click
        await ctx.page.waitForTimeout(2500);
      }

      ctx.log(`[agy-install] consent_signin done — atCallback=${atCallback} URL=${ctx.page.url().slice(0,80)}`);
    }, {
      verify:      async p => {
        const u = p.url();
        // Strict: only pass when we've actually reached the callback page
        return u.includes('antigravity.google') || u.includes('oauth-callback');
      },
      verifyLabel: 'Browser at antigravity.google/oauth-callback (auth code available)',
    });



    // ────────────────────────────────────────────────────────────────────────
    // Phase 7: Extract auth code from success page
    // ────────────────────────────────────────────────────────────────────────
    await ctx.testStep('copy_auth_code', async () => {
      // Wait for the code page (screenshot 5: "Paste this code into your application")
      await ctx.page.waitForFunction(
        () => document.body?.innerText?.includes('Paste this code') ||
              document.body?.innerText?.includes('code into your application') ||
              document.body?.innerText?.match(/4\/[0-9A-Za-z_\-\.]{10,}/),
        { timeout: 25_000 }
      ).catch(() => ctx.log('[agy-install] ⚠️ Timeout waiting for code page'));

      await ctx.page.waitForTimeout(1000);

      // ── DEBUG: Capture full page content for code format analysis ──
      const pageBodyFull = await ctx.page.evaluate(() => document.body?.innerText || '');
      ctx.log(`[agy-install] CALLBACK_PAGE_FULL_TEXT (first 600): ${pageBodyFull.slice(0,600).replace(/\n/g,' | ')}`);
      // Also dump to file on Colab for full inspection
      try {
        const { execSync: esDump } = await import('child_process');
        const { writeFileSync: wfsDump } = await import('fs');
        const dumpPath = '/tmp/agy_callback_page.txt';
        wfsDump(dumpPath, pageBodyFull, 'utf8');
        ctx.log(`[agy-install] Full page text saved to ${dumpPath} (${pageBodyFull.length} chars)`);
      } catch {}
      // Screenshot of the callback page
      try { await ctx.page.screenshot({ path: `${_evDir}/callback_page.png` }); ctx.log('📸 callback_page.png'); } catch {}

      // Strategy 0: Check page URL for ?code= param (fast-path, may not fire).
      // NOTE: antigravity.google/oauth-callback typically does NOT include ?code= in the final URL.
      // The code is consumed server-side by antigravity.google and displayed in the page BODY
      // (shown as a styled copyable box). Strategies 1-3 below (page body) are the reliable path.
      const pageUrl = ctx.page.url();
      ctx.log(`[agy-install] copy_auth_code page URL: ${pageUrl.slice(0, 100)}`);
      const urlCodeMatch = pageUrl.match(/[?&]code=([^&]+)/);
      if (urlCodeMatch) {
        authCode = decodeURIComponent(urlCodeMatch[1]).trim();
        ctx.log(`[agy-install] ✅ Auth code from URL (${authCode.length} chars)`);
      }

      if (!authCode) {
        authCode = await ctx.page.evaluate(() => {
          // Strategy 1: Look for a readonly input/textarea (the styled code box)
          const inputEl = document.querySelector('input[readonly], textarea[readonly]');
          if (inputEl?.value?.match(/^4\//)) return inputEl.value.trim();

          // Strategy 2: code/pre/span/div with 4/... pattern
          const codeEls = [...document.querySelectorAll('code, pre, [class*="code"], span, div')];
          for (const el of codeEls) {
            const t = (el.innerText || el.textContent || '').trim();
            if (/^4\/[A-Za-z0-9_\-\.]{20,}/.test(t)) return t;
          }

          // Strategy 3: Regex on full page text
          const m3 = document.body.innerText.match(/4\/[A-Za-z0-9_\-\.]{20,}/);
          if (m3) return m3[0].trim();

          // Strategy 4: ANY long token (relay code may not start with 4/)
          // Look for the styled code box content
          const codeBox = document.querySelector('[class*="code"],[class*="token"],[class*="auth"],[id*="code"]');
          if (codeBox) {
            const t = (codeBox.innerText || codeBox.textContent || '').trim();
            if (t.length > 20) return t;
          }
          // Strategy 5: Any 40+ char alphanumeric string  
          const m5 = document.body.innerText.match(/[A-Za-z0-9_\-\.]{40,}/);
          return m5 ? m5[0].trim() : null;
        });
      }


      if (!authCode) {
        // Try copying via button
        await ctx.page.evaluate(() => {
          const b = [...document.querySelectorAll('button')].find(el => (el.innerText || '').includes('Copy'));
          if (b) b.click();
        });
        await ctx.page.waitForTimeout(600);
        authCode = await ctx.page.evaluate(async () => {
          try { return (await navigator.clipboard.readText()) || null; }
          catch { return null; }
        });
      }

      if (!authCode) throw new Error('Could not extract auth code from success page. Check the screenshot.');
      ctx.log(`[agy-install] ✅ Auth code extracted (${authCode.length} chars)`);
    }, {
      verify:      async () => !!authCode,
      verifyLabel: 'Auth code extracted from browser',
    });

    // ────────────────────────────────────────────────────────────────────────
    // Phase 8: Send auth code to agy via codeSignal file
    // The PTY Python script (started in start_agy_auth) polls for this file.
    // When found, it writes the code to agy's PTY stdin. agy then does its
    // native PKCE token exchange AND registers the session with the Antigravity
    // backend — something a direct OAuth exchange cannot do.
    // ────────────────────────────────────────────────────────────────────────
    await ctx.step('paste_auth_code', async () => {
      if (!authCode)      throw new Error('No auth code from copy_auth_code');
      if (!ptyOutputFile) throw new Error('ptyOutputFile not set — start_agy_auth failed');

      // Derive codeSignal path from ptyOutputFile:
      //   ptyOutputFile = /tmp/agy_pty_<ts>
      //   codeSignal    = /tmp/agy_code_<ts>
      const codeSignalFile = ptyOutputFile.replace('/agy_pty_', '/agy_code_');
      ctx.log(`[agy-install] Writing code to PTY signal file: ${codeSignalFile}`);
      writeFileSync(codeSignalFile, authCode);
      ctx.log(`[agy-install] Code written (len=${authCode.length}). Waiting for agy to exchange...`);

      // Wait up to 180s for agy to write the token file (agy exchanges + Antigravity backend setup)
      const deadline = Date.now() + 180_000;
      let tokenWritten = false;
      const { execSync: esToken } = await import('child_process');
      const checkToken = () => {
        try {
          const raw = esToken(`cat "${AGY_TOKEN_FILE}" 2>/dev/null || echo ""`,
            { encoding: 'utf8', timeout: 3000, env: AGY_CMD_ENV() });
          return raw.includes('refresh_token') || raw.includes('access_token');
        } catch { return false; }
      };

      // Poll every 5s
      while (Date.now() < deadline && !tokenWritten) {
        await new Promise(r => setTimeout(r, 5_000));
        tokenWritten = checkToken();
        if (tokenWritten) {
          ctx.log('[agy-install] ✅ Token file written by agy!');
        } else {
          // Log PTY output snippet every 30s for diagnostics
          const elapsed = Math.round((Date.now() - (deadline - 180_000)) / 1000);
          if (elapsed % 30 < 6) {
            try {
              const ptyLog = readFileSync(`${ptyOutputFile}.log`, 'utf8');
              ctx.log(`[agy-install][${elapsed}s] PTY tail: ${ptyLog.slice(-300).replace(/\n/g,' | ')}`);
            } catch {}
          }
        }
      }

      if (!tokenWritten) {
        // Last-ditch: check if PTY log mentions TOKEN_WRITTEN (might be in non-standard path)
        try {
          const ptyLog = readFileSync(`${ptyOutputFile}.log`, 'utf8');
          if (ptyLog.includes('TOKEN_WRITTEN')) {
            ctx.log('[agy-install] PTY log shows TOKEN_WRITTEN — checking all gemini files...');
            tokenWritten = true; // verify_auth will do thorough check
          } else {
            ctx.log(`[agy-install] PTY log tail (last 600): ${ptyLog.slice(-600)}`);
          }
        } catch {}
      }

      if (!tokenWritten)
        throw new Error('agy did not write token file within 180s of code signal');
    });


  } // end auth phases

  // ──────────────────────────────────────────────────────────────────────────
  // Phase 9: Verify authentication
  // ──────────────────────────────────────────────────────────────────────────
  await ctx.step('verify_auth', async () => {
    if (binaryOnly) { ctx.log('[agy-install] binary_only — skipping auth verify'); return; }

    // Token was written directly by paste_auth_code — short wait to ensure file is flushed
    await new Promise(r => setTimeout(r, 5_000));

    // Discover token files across ALL of ~/.gemini (agy may store in subdirs)
    const { execSync: es2 } = await import('child_process');
    const geminiDir = AGY_GEMINI_DIR;  // module-level constant, no os import needed

    const keyringFiles = (() => { try { return es2(`find /root/.local/share/keyrings -type f 2>/dev/null || true`, { encoding: 'utf8', timeout: 3000 }).trim().split('\n').filter(Boolean); } catch { return []; } })();
    const allGeminiFiles = (() => {
      try {
        return es2(`find ${JSON.stringify(geminiDir)} -type f 2>/dev/null`,
          { encoding: 'utf8', timeout: 5000 }
        ).trim().split('\n').filter(Boolean);
      } catch { return []; }
    })();

    const newFiles = (() => {
      try {
        return es2(
          `find ${JSON.stringify(AGY_GEMINI_DIR)} -type f -newer ${JSON.stringify(AGY_BIN)} 2>/dev/null`,
          { encoding: 'utf8', timeout: 5000 }
        ).trim().split('\n').filter(Boolean);
      } catch { return []; }
    })();
    if (newFiles.length) ctx.log(`[agy-install] New files after auth: ${newFiles.join(', ')}`);

    // Search for token content across all ~/.gemini files
    const stateFiles = (() => { try { return readdirSync(AGY_STATE_DIR).map(f => `${AGY_STATE_DIR}/${f}`); } catch { return []; } })();
    const tokenPaths = [...new Set([AGY_TOKEN_FILE, ...stateFiles, ...newFiles, ...allGeminiFiles, ...keyringFiles])]
      .filter(f => { try { return existsSync(f); } catch { return false; } });

    let tokenFound = false;
    let tokenPath = '';
    for (const tp of tokenPaths) {
      try {
        const raw = readFileSync(tp, 'utf8');
        if (raw.includes('refresh_token') || raw.includes('access_token') || raw.includes('oauth')) {
          ctx.log(`[agy-install] ✅ Token found at: ${tp}`);
          tokenFound = true; tokenPath = tp;
          break;
        }
      } catch {}
    }

    // Run a real test prompt — use script -c to allocate a PTY (agy is interactive)
    let agyWorks = false;
    let testOut = '';
    try {
      testOut = es2(
        `script -q -c 'echo "respond with exactly: OK" | timeout 30 ${JSON.stringify(AGY_BIN)}' /dev/null 2>&1 || true`,
        { env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 35_000, shell: true }
      );
      ctx.log(`[agy-install] agy prompt test: ${testOut.slice(0, 300)}`);
      agyWorks = !testOut.toLowerCase().includes('not signed in') &&
                 !testOut.toLowerCase().includes('not logged') &&
                 !testOut.toLowerCase().includes('you are not') &&
                 testOut.trim().length > 3;
    } catch (e) {
      ctx.log(`[agy-install] agy prompt test error: ${e.message.slice(0, 100)}`);
    }

    if (!agyWorks) {
      // agy is not authenticated with the Antigravity backend.
      // This happens when only a PKCE Google token exists (missing backend registration).
      // Delete the stale token so the next run goes through the full PTY auth flow.
      if (tokenFound) {
        ctx.log('[agy-install] ⚠️ Token file exists but agy not authenticated with Antigravity — deleting stale token');
        try { unlinkSync(AGY_TOKEN_FILE); } catch {}
      }
      throw new Error(`AGY not authenticated with Antigravity backend. Prompt test: ${testOut.slice(0, 200)}`);
    }

    const ver = (() => { try { return es2(`${JSON.stringify(AGY_BIN)} --version`, { env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 10_000 }).trim(); } catch { return 'unknown'; } })();
    ctx.log(`[agy-install] ✅ AGY authenticated: ${ver} | token=${tokenFound}@${tokenPath || 'n/a'} | prompt=${agyWorks}`);
  });


  // ──────────────────────────────────────────────────────────────────────────
  // Phase 10: Persist to R2
  // ──────────────────────────────────────────────────────────────────────────
  await ctx.step('persist_to_r2', async () => {
    // Always persist binary (cheap, idempotent)
    if (existsSync(AGY_BIN)) {
      try {
        await r2Op('put', r2BinKey, AGY_BIN, ctx.log.bind(ctx));
        ctx.log(`[agy-install] ✅ Binary persisted to R2`);
      } catch (e) {
        ctx.log(`[agy-install] ⚠️ Binary persist failed: ${e.message}`);
      }
    }

    if (binaryOnly) { ctx.log('[agy-install] binary_only — skipping credential persist'); return; }
    if (!isAgyAuthed() && !credRestored) { ctx.log('[agy-install] No credential to persist'); return; }

    ctx.log('[agy-install] Bundling and encrypting credential files...');
    const ts     = Date.now();
    const tarTmp = `/tmp/agy_bundle_${ts}.tar.gz`;
    const encTmp = `/tmp/agy_bundle_${ts}.tar.gz.enc`;

    try {
      // Persist entire ~/.gemini/ dir (composite_token_storage, jetski_state, config)
      const excludes = [
        "--exclude='**/log/**'",
        "--exclude='**/crashes/**'",
        "--exclude='**/conversation_summaries.db'",
        "--exclude='**/builtin/**'",
        "--exclude='**/webm_encoder'",
      ].join(' ');
      execSync(
        `tar ${excludes} -czf ${JSON.stringify(tarTmp)} -C ${JSON.stringify(HOME)} .gemini`,
        { stdio: 'pipe' }
      );
      const bundleSize = (() => { try { return execSync(`stat -c%s ${JSON.stringify(tarTmp)} 2>/dev/null || stat -f%z ${JSON.stringify(tarTmp)}`, { encoding: 'utf8' }).trim(); } catch { return '?'; } })();
      ctx.log(`[agy-install] Bundle: ${bundleSize}b (.gemini/ dir)`);
      cryptFile('enc', tarTmp, encTmp);

      const size = execSync(`stat -c%s ${JSON.stringify(encTmp)} 2>/dev/null || stat -f%z ${JSON.stringify(encTmp)}`, { encoding: 'utf8' }).trim();
      ctx.log(`[agy-install] Bundle: ${size}b encrypted`);

      await r2Op('put', r2CredKey, encTmp, ctx.log.bind(ctx));
      ctx.log(`[agy-install] ✅ Credential persisted to R2: ${r2CredKey}`);
    } catch (e) {
      ctx.log(`[agy-install] ⚠️ Persist failed: ${e.message}`);
    } finally {
      try { unlinkSync(tarTmp); } catch {}
      try { unlinkSync(encTmp); } catch {}
    }

  });

  ctx.log('[agy-install] 🎉 Workflow complete');
  return {
    ok:              true,
    authenticated:   !binaryOnly,
    restored_from_r2: credRestored,
    r2_cred_key:     r2CredKey,
    r2_binary_key:   r2BinKey,
    agy_version:     (() => { try { return execSync(`${JSON.stringify(AGY_BIN)} --version`, { env: AGY_CMD_ENV(), encoding: 'utf8', timeout: 5000 }).trim(); } catch { return 'unknown'; } })(),
  };
}
