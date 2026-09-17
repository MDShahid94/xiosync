/**
 * Runtime Manager — three MCP tools for live runtime control:
 *   xb_runtime_config   — read/write per-node or global runtime config on Drive
 *   xb_runtime_disconnect — gracefully disconnect a runtime (release locks, unassign)
 *   xb_runtime_spawn    — manually trigger self-spawn with a desired session
 */
import { execSync, spawn } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { getD1Client } from '../core/d1.mjs';

const LOCAL_ROOT    = process.env.XIO_LOCAL_ROOT ?? '/content/xio-mesh';
const XIOBR_PORT   = process.env.XIOBR_PORT     ?? '4242';
const NODE_NAME    = process.env.XIO_NODE_NAME  ?? 'colab-master';
const CONFIG_PATH  = '/tmp/xio_config.json';

// Whitelisted config keys settable at runtime
const LIVE_KEYS = new Set([
  'auto_spawn_enabled', 'auto_spawn_after_minutes', 'auto_spawn_google_signin',
  'lock_stale_minutes', 'job_retention_days', 'default_exit',
]);

function readLocalConfig() {
  try { return JSON.parse(readFileSync(CONFIG_PATH, 'utf8')); } catch { return {}; }
}

// ── Drive config listing via boot.py (only for 'list' action) ─────────────
// NOTE: Only used for Drive folder listing. All config R/W uses Supabase
// to avoid the ~60s boot.py startup ETIMEDOUT issue.
function pyDriveList(timeoutMs = 8000) {
  try {
    return execSync(
      `python3 -c "
import sys, json
sys.path.insert(0, '/content/xio-browser/colab')
from boot import svc, FOLDER_ID
results = svc.files().list(
    q=\\"'\" + FOLDER_ID + \"' in parents and trashed=false and name contains 'xio_config'\\",
    spaces='drive', fields='files(name,id,modifiedTime)', pageSize=50,
    supportsAllDrives=True, includeItemsFromAllDrives=True
).execute().get('files', [])
print(json.dumps(results))
"`,
      { encoding: 'utf8', timeout: timeoutMs }
    ).trim();
  } catch { return '[]'; }
}

// ── D1-backed config store ──────────────────────────────────────────────────
// Config is stored in D1 table: runtime_config(key, value, scope)
// Replaces Supabase runtime_configs JSONB table.

async function _d1GetConfig(d1Scope) {
  try {
    const d1 = getD1Client();
    return await d1.getConfig(d1Scope);   // merged global + node-specific
  } catch { return {}; }
}

async function _d1SetConfig(key, value, d1Scope) {
  const d1 = getD1Client();
  await d1.setConfig(key, String(value), d1Scope);
}

async function _d1ListAllConfig() {
  try {
    const d1 = getD1Client();
    return await d1.select('runtime_config', { order: 'scope, key' });
  } catch { return []; }
}

// ── xb_runtime_config ──────────────────────────────────────────────────────
export const runtimeConfigTool = {
  name: 'xb_runtime_config',
  description:
    'Read or write XIO Mesh runtime configuration. Changes are persisted to D1 ' +
    '(runtime_config table) and applied live immediately (local node) or within 2 minutes (remote). ' +
    'Use scope="global" to change all runtimes; scope="node" (default) for this runtime only.',
  inputSchema: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['get', 'set', 'list'], description: 'Action: get current config, set a key, or list all node configs.' },
      key:    { type: 'string', description: 'Config key to get or set (e.g. auto_spawn_enabled, auto_spawn_after_minutes).' },
      value:  { description: 'Value to set (boolean, number, or string).' },
      scope:  { type: 'string', enum: ['node', 'global'], description: 'node = this runtime only (default); global = all runtimes.' },
      node:   { type: 'string', description: 'Target node name (default: this node). For set, writes to that node\'s config.' },
    },
    required: ['action'],
  },
  async handler({ action, key, value, scope = 'node', node } = {}) {
    const targetNode = node ?? NODE_NAME;
    const d1Scope    = scope === 'global' ? 'global' : targetNode;

    if (action === 'get') {
      const local     = readLocalConfig();
      const d1Cfg     = await _d1GetConfig(d1Scope);
      const merged    = { ...local, ...d1Cfg };
      if (key) {
        return { node: targetNode, key, value: merged[key] ?? null };
      }
      return { node: targetNode, effective_config: merged };
    }

    if (action === 'set') {
      if (!key)             throw new Error('key is required for set');
      if (value === undefined) throw new Error('value is required for set');
      if (!LIVE_KEYS.has(key)) throw new Error(`Key "${key}" is not runtime-settable. Allowed: ${[...LIVE_KEYS].join(', ')}`);

      await _d1SetConfig(key, value, d1Scope);

      // Immediate in-process apply on local node via /config endpoint (no 2-min wait)
      let appliedNow = false;
      if (d1Scope === 'global' || d1Scope === NODE_NAME) {
        try {
          const resp = await fetch(`http://localhost:${XIOBR_PORT}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value }),
            signal: AbortSignal.timeout(3000),
          });
          appliedNow = resp.ok;
        } catch { /* non-critical — keep-alive will apply within 2 min */ }
      }

      return { ok: true, node: targetNode, scope: d1Scope, key, new_value: value,
               applied_immediately: appliedNow,
               note: appliedNow
                 ? 'Change applied immediately to running process.'
                 : 'Change persisted to D1. Will be applied within 2 minutes (keep-alive refresh).' };
    }

    if (action === 'list') {
      const rows = await _d1ListAllConfig();
      let driveFiles = [];
      try { driveFiles = JSON.parse(pyDriveList()); } catch {}
      return { all_config_rows: rows, drive_config_files: driveFiles };
    }

    throw new Error(`Unknown action: ${action}`);
  },
};

// ── xb_runtime_disconnect ──────────────────────────────────────────────────
export const runtimeDisconnectTool = {
  name: 'xb_runtime_disconnect',
  description:
    'Gracefully disconnect a Colab runtime: release all Drive locks and unassign the runtime. ' +
    'Use node="self" (default) for this runtime or specify a remote node name. ' +
    'Equivalent to the old Cell 4 (Soft-Unlock & Disconnect).',
  inputSchema: {
    type: 'object',
    properties: {
      node:   { type: 'string', description: 'Node to disconnect: "self" (default) or a node name like "colab-worker-karmareturnsfromallsides".' },
      reason: { type: 'string', description: 'Optional reason for disconnection (logged).' },
    },
  },
  async handler({ node = 'self', reason = 'manual disconnect' } = {}) {
    const targetNode = node === 'self' ? NODE_NAME : node;
    const isLocal = (targetNode === NODE_NAME || node === 'self');

    if (isLocal) {
      // Step 1: Release all Drive locks
      try {
        execSync(
          'python3 /content/xio-browser/colab/boot.py --unlock-all',
          { encoding: 'utf8', timeout: 20000, stdio: 'pipe' }
        );
      } catch (_) {}

      // Step 2: Terminate the Jupyter kernel via REST API (graceful) + pkill fallback.
      // NOTE: google.colab.runtime.unassign() only works from within the Jupyter kernel
      // context — a detached subprocess has no kernel connection and silently fails.
      // The reliable approach is to delete the kernel via the Jupyter HTTP API.
      spawn('bash', ['-c', `
        # Try Jupyter REST API on port 9000 (Colab default) or 8888
        for PORT in 9000 8888; do
          KERNELS=$(curl -s --max-time 3 http://localhost:$PORT/api/kernels 2>/dev/null)
          KID=$(echo "$KERNELS" | python3 -c "
import sys, json
try:
    ks = json.load(sys.stdin)
    if ks: print(ks[0]['id'])
except: pass
" 2>/dev/null)
          if [ -n "$KID" ]; then
            curl -s -X DELETE http://localhost:$PORT/api/kernels/$KID 2>/dev/null
            echo "Deleted kernel $KID on port $PORT"
            break
          fi
        done
        # Fallback: kill the ipykernel process directly
        sleep 1
        pkill -f ipykernel_launcher 2>/dev/null || true
      `], { detached: true, stdio: 'ignore' }).unref();

      return { ok: true, node: targetNode, reason, message: 'Locks released. Runtime disconnecting…' };
    }

    // Remote disconnect via /disconnect HTTP endpoint (option A — professional approach)
    const registryOut = execSync(
      `python3 -c "
import sys, json
sys.path.insert(0, '/content/xio-browser/colab')
from boot import drive_download
import tempfile, os
tmp = tempfile.mktemp()
drive_download('registry/${targetNode}.json', tmp)
if os.path.exists(tmp):
    with open(tmp) as f: data = json.load(f)
    print(data.get('ts_ip', ''))
else:
    print('')
try: os.unlink(tmp)
except: pass
"`,
      { encoding: 'utf8', timeout: 15000 }
    ).trim();

    if (!registryOut) {
      throw new Error(`Cannot find registry entry for node: ${targetNode}. Is it online?`);
    }
    const resp = await fetch(`http://${registryOut}:${XIOBR_PORT}/disconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
      signal: AbortSignal.timeout(15000),
    });
    const body = await resp.json();
    return { ok: body.ok ?? false, node: targetNode, remote_ip: registryOut, response: body };
  },
};

// ── xb_runtime_spawn ──────────────────────────────────────────────────────
export const runtimeSpawnTool = {
  name: 'xb_runtime_spawn',
  description:
    'Manually trigger a self-spawn workflow on this runtime (or a remote node), ' +
    'optionally specifying which session profile to use. Returns the launched job_id. ' +
    'If no session_id is given, auto-picks the next available Pro session from Supabase.',
  inputSchema: {
    type: 'object',
    properties: {
      session_id: { type: 'string', description: 'Session to spawn with (email or slug). Default: auto-pick next available Pro session.' },
      node:       { type: 'string', description: 'Node to trigger spawn on: "self" (default) or a remote node name.' },
      params:     { type: 'object', description: 'Extra params forwarded to the self-spawn workflow (e.g. auto_spawn_google_signin).' },
    },
  },
  async handler({ session_id, node = 'self', params = {} } = {}) {
    const targetNode = node === 'self' ? NODE_NAME : node;
    const isLocal    = (targetNode === NODE_NAME || node === 'self');

    // Auto-pick next available Pro session if none specified
    let _sid = session_id;
    if (!_sid) {
      try {
        const { listAccounts } = await import('../core/db.mjs');
        const accounts = listAccounts().filter(a => a.is_active !== false && a.tier === 'pro');
        if (accounts.length > 0) _sid = accounts[0].email;
      } catch {}
      if (!_sid) _sid = 'karmareturnsfromallsides'; // absolute fallback
    }

    const callBody = JSON.stringify({
      tool: 'xb_run_workflow',
      args: { workflow: 'self-spawn', session_id: _sid, params },
    });

    let targetUrl = `http://localhost:${XIOBR_PORT}/call`;
    if (!isLocal) {
      const registryOut = execSync(
        `python3 -c "
import sys, json
sys.path.insert(0, '/content/xio-browser/colab')
from boot import drive_download
import tempfile, os
tmp = tempfile.mktemp()
drive_download('registry/${targetNode}.json', tmp)
if os.path.exists(tmp):
    with open(tmp) as f: data = json.load(f)
    print(data.get('ts_ip', ''))
else: print('')
try: os.unlink(tmp)
except: pass
"`, { encoding: 'utf8', timeout: 15000 }
      ).trim();
      if (!registryOut) throw new Error(`Cannot find registry for node: ${targetNode}`);
      targetUrl = `http://${registryOut}:${XIOBR_PORT}/call`;
    }

    const resp = await fetch(targetUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: callBody,
      signal: AbortSignal.timeout(20000),
    });
    const result = await resp.json();
    return { ok: result.ok, node: targetNode, session_id: _sid, job: result.result };
  },
};

// ── xb_save_contexts ──────────────────────────────────────────────────────
export const saveContextsTool = {
  name: 'xb_save_contexts',
  description: 'Gracefully close all active Chrome browser contexts on the running server and save their session states (cookies, localStorage) to disk.',
  inputSchema: {
    type: 'object',
    properties: {},
  },
  async handler() {
    const { listActiveContexts, closeContext } = await import('../core/browser-pool.mjs');
    const ids = listActiveContexts();
    await Promise.allSettled(ids.map(id => closeContext(id)));
    return { ok: true, saved_count: ids.length, session_ids: ids };
  },
};
