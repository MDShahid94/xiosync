import fs from 'node:fs';
import { execSync } from 'node:child_process';
import { normaliseSessionId } from '../utils/session-id.mjs';

/**
 * Handles the 3-tier fallback mechanism when a session is found to be signed-out.
 * Finds an alternative Pro account and triggers a chained job.
 *
 * NOTE: We deliberately do NOT evict/destroy cookies here. Eviction permanently
 * marks is_persisted=0 which syncs to Supabase and orphans the session forever.
 * Instead, the session is left intact so future rescue-pulls from Supabase work.
 *
 * @param {object} ctx - The workflow context
 * @param {string} targetWorkflow - The workflow to execute with the new session
 * @param {object} targetParams - Optional params for the target workflow
 * @throws {Error} Always throws to terminate the current executing workflow
 */
export async function handleSessionFallback(ctx, targetWorkflow, targetParams = {}) {
  ctx.log(`⚠️ Session for ${ctx.sessionId} is inactive — searching for fallback (cookies preserved for rescue)...`);
  // NOTE: intentionally NOT calling evictDomainFromSession or evictLocalProfileCookies.
  // Those calls permanently orphan sessions in Supabase. Sessions are kept intact
  // so a fresh ensureSessionState() pull on the next attempt can rescue them.


    const pyScript = `
import json, urllib.request, sys, time, os
sys.path.append('/content/xio-browser/colab')
try:
    import sync
except Exception:
    sync = None

# ── Load CF D1 credentials ────────────────────────────────────────────────────
try:
    with open('/tmp/xio_config.json') as f:
        cfg = json.load(f)
    cf_token   = cfg.get('cf_api_token', '')   or os.environ.get('CF_API_TOKEN', '')
    cf_account = cfg.get('cf_account_id', '')  or os.environ.get('CF_ACCOUNT_ID', '')
    cf_db      = cfg.get('cf_d1_database_id', '') or os.environ.get('CF_D1_DATABASE_ID', '')
except Exception:
    sys.exit(1)

if not cf_token or not cf_account or not cf_db:
    sys.exit(1)

def d1_query(sql, params=None):
    url = f'https://api.cloudflare.com/client/v4/accounts/{cf_account}/d1/database/{cf_db}/query'
    body = json.dumps({'sql': sql, 'params': params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Authorization': f'Bearer {cf_token}',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    if not data.get('success'):
        raise Exception(f'D1 error: {data}')
    return data['result'][0]['results']

def is_locked(rel_path):
    # Check Drive file lock property (best-effort; returns False on any error)
    try:
        _fname = rel_path.rsplit('/', 1)[-1]
        if not (sync and hasattr(sync, 'svc') and sync.svc):
            return False
        # Search Drive for the state file to get its ID
        _res = sync.svc.files().list(
            q=f'name="{_fname}" and trashed=false',
            spaces='drive', fields='files(id,properties)', pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute().get('files', [])
        if not _res:
            return False
        props = _res[0].get('properties', {})
        if 'locked_by' in props:
            t = float(props.get('locked_at', 0))
            if time.time() - t < 35 * 60:   # 35 min TTL matches boot.py
                return True
    except Exception:
        pass
    return False

try:
    # ── Query D1: sessions + Pro accounts ────────────────────────────────────
    # D1 sessions table has no 'tier' column — tier lives in accounts table.
    sessions = d1_query('SELECT id, serial, is_persisted, exit_node FROM sessions')
    accounts = d1_query("SELECT email, tier, password, totp_secret FROM accounts WHERE is_active = 1 AND tier = 'Pro'")

    # Set of persisted session IDs
    persisted_sids = {
        s['id'] for s in sessions
        if s.get('is_persisted') in (True, 1, '1', 'true', 'True')
    }

    # Pro accounts that have a persisted session (matched by email username)
    persisted_accounts = []
    for acc in accounts:
        eml = acc.get('email', '')
        if not eml or '@' not in eml:
            continue
        username = eml.split('@')[0].split('+')[0]
        username_norm = username.replace('.', '_').replace('-', '_')
        if username in persisted_sids or username_norm in persisted_sids:
            persisted_accounts.append(acc)

    persisted_accounts.sort(key=lambda x: x.get('email', '').lower())
    persisted_emails = [acc['email'] for acc in persisted_accounts]

    # Reorder: start after current session (round-robin)
    current_email = '${ctx.sessionId}' if '@' in '${ctx.sessionId}' else '${ctx.sessionId}@gmail.com'
    try:
        idx = persisted_emails.index(current_email)
        ordered_candidates = persisted_emails[idx+1:] + persisted_emails[:idx]
    except ValueError:
        ordered_candidates = [x for x in persisted_emails if x != current_email]

    # Find first unlocked persisted Pro account
    valid_fallback = None
    for eml in ordered_candidates:
        uname = eml.split('@')[0].split('+')[0].replace('.', '_').replace('-', '_')
        state_path = f'tailscale_states/TS_colab_worker_{uname}.state'
        if not is_locked(state_path):
            valid_fallback = eml
            break

    if valid_fallback:
        print(valid_fallback)
        sys.exit(0)

    # Tier 3: unpersisted Pro account (fresh onboarding)
    persisted_set = set(persisted_emails)
    unpersisted = [acc for acc in accounts if acc.get('email') not in persisted_set]
    unpersisted.sort(key=lambda x: x.get('email', '').lower())
    if unpersisted:
        row = unpersisted[0]
        pwd  = row.get('password', '')
        totp = row.get('totp_secret', '')
        print(f"NEW:{row['email']}:{pwd}:{totp}")
        sys.exit(0)

    sys.exit(1)

except Exception as e:
    sys.exit(1)
`;

  const b64 = Buffer.from(pyScript).toString('base64');
  let fallbackOutput = '';
  try {
    fallbackOutput = execSync(`python3 -c "import base64; exec(base64.b64decode('${b64}'))"`, { encoding: 'utf8' }).trim();
  } catch (e) {}

  // Scan output lines for the expected result format
  // Python may emit warnings before the actual output — don't blindly take last line.
  const lines = fallbackOutput.split('\n').map(l => l.trim()).filter(Boolean);
  const fallbackResult = lines.find(l => l.startsWith('NEW:') || /^[\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,}$/.test(l)) ?? null;

  if (fallbackResult?.startsWith('NEW:')) {
    const [_unused, eml, pwd, totp] = fallbackResult.split(':');
    ctx.log(`Fallback Step 3: Triggering google-signin for unused Pro account ${eml}`);
    
    // Tier 3: use the full email as session ID — session-manager.mjs uses it to derive
    // the canonical PRFL-NNN_username.json / PRFL-NNN_username.tar.gz paths via the DB serial.
    const newSid = eml;  // full email, e.g. "kamnurtalukdar@gmail.com"
    const paramsStr = JSON.stringify(targetParams).replace(/"/g, '\\"');
    
    // Detached bash script to chain google-signin -> targetWorkflow
    // Read XIOBR_PORT from config — never hardcode port
    const bashScript = `#!/bin/bash
XIOBR_PORT=$(python3 -c "import json; c=json.load(open('/tmp/xio_config.json')); print(c.get('http_port', 4242))" 2>/dev/null || echo 4242)
JOB_ID=$(curl -s -X POST http://127.0.0.1:$XIOBR_PORT/call -H "Content-Type: application/json" -d '{"tool":"xb_run_workflow","args":{"workflow":"google-signin","session_id":"${newSid}","params":{"email":"${eml}","password":"${pwd}","totp_secret":"${totp}"}}}' | jq -r '.result.job_id')
if [ "$JOB_ID" == "null" ] || [ -z "$JOB_ID" ]; then exit 1; fi
while true; do
  STATUS=$(curl -s -X POST http://127.0.0.1:$XIOBR_PORT/call -H "Content-Type: application/json" -d '{"tool":"xb_job_poll","args":{"job_id":"'$JOB_ID'"}}' | jq -r '.result.status')
  if [ "$STATUS" == "done" ]; then
    curl -s -X POST http://127.0.0.1:$XIOBR_PORT/call -H "Content-Type: application/json" -d '{"tool":"xb_run_workflow","args":{"workflow":"${targetWorkflow}","session_id":"${newSid}","params":${paramsStr}}}'
    break
  elif [ "$STATUS" == "error" ] || [ "$STATUS" == "cancelled" ]; then
    break
  fi
  sleep 5
done
`;
    fs.writeFileSync('/tmp/fallback_chain.sh', bashScript);
    execSync('chmod +x /tmp/fallback_chain.sh');
    import('node:child_process').then(({ spawn }) => {
      const child = spawn('/tmp/fallback_chain.sh', [], { detached: true, stdio: 'ignore' });
      child.unref();
    });
    
    throw new Error(`Session invalid. Auto-started google-signin chain for unused fallback: ${eml}`);
  } else if (fallbackResult) {
    ctx.log(`Fallback Step 1: Found unlocked persisted Pro account: ${fallbackResult}`);
    const paramsStr = JSON.stringify(targetParams);
    
    import('node:child_process').then(({ spawn }) => {
      const child = spawn('curl', [
        '-s', '-X', 'POST',
        `http://127.0.0.1:${process.env.XIOBR_PORT ?? 4242}/call`,
        '-H', 'Content-Type: application/json',
        '-d', `{"tool":"xb_run_workflow","args":{"workflow":"${targetWorkflow}","session_id":"${fallbackResult}","params":${paramsStr}}}`
      ], { detached: true, stdio: 'ignore' });
      child.unref();
    });
    
    throw new Error(`Session invalid. Chained execution triggered with fallback session: ${fallbackResult}`);
  } else {
    throw new Error("Session invalid and no fallback Pro accounts available for self-spawn.");
  }
}