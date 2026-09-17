---
name: xio-mesh-tools
description: >
  Reference guide for the XIO Mesh / XIO Browser orchestration system.
  Use this skill whenever you need to control a Colab runtime, run workflows,
  restart the MCP server, inspect jobs, interact with the live browser, apply
  hot-patches, or manage sessions and exit nodes.
  The system exposes all tools via HTTP at http://<tailscale-ip>:<port>/call.
---

# XIO Mesh Tools — Complete Agent Reference

## What Is XIO Mesh?

XIO Mesh is a **self-healing, multi-node Colab orchestration system** that:
- Runs Chrome automation workflows on Colab runtimes reachable over Tailscale VPN
- Persists ALL state to a hybrid fast+cold storage:
  - **Chrome profiles** → **Cloudflare R2** (`xio-mesh` bucket, S3 API, ~2 s push/pull) + Google Drive (cold backup)
  - **Session cookies/localStorage/IndexedDB** → **Cloudflare D1** `sessions.cookie_state` (primary) + Drive (cold backup)
  - **Tailscale identities** → **Cloudflare R2** `ts_states/TS_{node}.state` (primary) + Drive (cold backup)
  - **Registry, accounts, credentials** → **Cloudflare D1** (permanent, cross-runtime)
  - **Jobs, drive_assets** → Local SQLite `/content/xio-mesh/xio-browser.db` (ephemeral per runtime)
- Survives Colab runtime resets via v2 soft-persistence (JSON-only session recovery, no tarball needed)
- Scales horizontally via self-spawned worker nodes, each with their own Tailscale identity

```
┌──────────────────────────────────────────────────────────────────────┐
│                       XIO MESH ARCHITECTURE                          │
│                                                                      │
│  Mac (owner)  ──tailscale──▶  Colab Master Node (colab-master)       │
│                               • start.ipynb → fetches boot.py        │
│                               • MCP server (xio-browser, port 4242)  │
│                               • Chrome browser (patchright)           │
│                               • Tailscale daemon                      │
│                               • TS identity in R2 ts_states/         │
│                               │                                        │
│                               └── self-spawn wf ──▶  Worker Nodes     │
│                                   colab-worker-<username>             │
│                                   • own R2 TS identity               │
│                                   • NO patchright (workers skip it)  │
│                                                                       │
│  Chrome profiles:  Cloudflare R2 xio-mesh (primary) + Drive (cold)  │
│  Session cookies:  Cloudflare D1  (primary) + Drive (cold backup)    │
│  TS identities:    Cloudflare R2  (primary) + Drive (cold backup)    │
│  Registry/DB:      Cloudflare D1  (permanent cross-runtime)          │
│  Jobs/assets:      Local SQLite   (ephemeral per runtime)            │
│  Network mesh:     Tailscale (100.x.x.x addresses)                   │
└──────────────────────────────────────────────────────────────────────┘
```

**Critical facts every agent must know:**
- MCP server port is **4242 by default**. Always read actual port from `/tmp/xio_config.json` → `xiobr_port`.
- All tools: `POST /call` with `{tool, args}`. No SSE/streaming required.
- **Chrome profiles**: primary store is **Cloudflare R2** (`xio-mesh` bucket). Drive is cold backup only.
- **Session cookies/localStorage/IndexedDB**: primary store is **D1** `sessions.cookie_state`. Drive is cold backup.
- **Tailscale states**: primary store is **R2** `ts_states/TS_{node}.state`. Drive is fallback.
- **Accounts, sessions, credentials, config**: **Cloudflare D1** `xio-mesh` database (permanent, cross-runtime).
- Session files follow canonical naming: `PRFL-NNN_<username>.json` (e.g. `PRFL-003_shahid_raiganj.json`).
- Chrome profiles stored as `PRFL-NNN_<username>.tar.gz` in **R2** `chrome_profiles/`; cold-backed to Drive.
- The master node is detected by **email NOT being in the D1 `accounts` table** — no flag files or URL params.
- v2 session files are **self-sufficient for recovery** — the `.tar.gz` tarball is optional (bonus, not required).
- Job evidence is hierarchical: `steps/` (local only, never pushed) + nested sub-workflow dirs on Drive.
- **Hybrid DB**: D1 for accounts/sessions/credentials (cross-runtime), local SQLite for jobs (ephemeral only).
- **R2 for Chrome profiles**: `r2_endpoint`, `r2_access_key_id`, `r2_secret_access_key`, `r2_bucket` are loaded from D1 `node_secrets` at boot. If missing, system falls back to Drive.
- **Pro sessions (13 total, all persisted)**: `1x2xx3xxx4xxxx5xxxxx6xxxxxx789`, `karmareturnsfromallsides`, `shahid.raiganj`, `sarvamekam1`, `xiosyncnetwork`, `samnurnihartalukdar`, `thewitnessone`, `shahid.workload`, `lailakhatun78693`, `lailakhatun733156`, `chaina.anantapurasha`, `whatisdeath2013`, `samnur.raiganj` (in serial order — matches `SPAWN_ACCOUNTS` in `colab/spawner.py`).

---

## 1. Discover The Live Node

**Confirm server online:**
```bash
PORT=$(python3 -c "import json; c=json.load(open('/tmp/xio_config.json')); print(c.get('xiobr_port',4242))" 2>/dev/null || echo 4242)
curl -s http://100.120.53.61:$PORT/health | jq '{ok, version, port, running_job}'
```

**Key fields in `/tmp/xio_config.json`:**
```json
{
  "node_name":                   "colab-master",
  "runtime_email":               "owner@gmail.com",
  "is_worker":                   false,
  "xiobr_port":                  4242,
  "default_exit":                "100.86.149.127",
  "shared_folder_id":            "...",
  "local_root":                  "/content/xio-mesh",
  "cf_api_token":                "cfat_...",
  "cf_account_id":               "e63a13ef...",
  "cf_d1_database_id":           "acfd72de-...",
  "indexeddb_origins":           ["https://accounts.google.com","https://myaccount.google.com","https://www.google.com","https://v0.dev"],
  "localstorage_min_cookies":    5,
  "warmup_origins":              ["https://accounts.google.com","https://myaccount.google.com"],
  "auto_spawn_enabled":          true,
  "auto_spawn_after_minutes":    12,
  "auto_spawn_google_signin":    "auto",
  "lock_stale_minutes":          35,
  "job_retention_days":          3,
  "mac_specs":                   {"cores":10,"ram":16,"...":"..."}
}
```

> **D1 credentials** (`cf_api_token`, `cf_account_id`, `cf_d1_database_id`) are injected into the Node.js process as env vars by `boot.py` via `XIOBR_CMD`.
> MCP restart (boot.py keepalive, restart.py, xb_restart) all work correctly.

**List all mesh peers:**
```bash
curl -s -X POST http://100.120.53.61:4242/call \
  -H "Content-Type: application/json" \
  -d '{"tool":"xb_node_status","args":{}}' \
  | jq '.result.all_peers[] | {hostname, primary_ip, online}'
```

---

## 2. Calling Any Tool

**Universal format:**
```bash
curl -s -X POST http://<IP>:<PORT>/call \
  -H "Content-Type: application/json" \
  -d '{"tool":"<TOOL_NAME>","args":{...}}' | jq '.result'
```

**Response shape:**
```json
{ "ok": true,  "result": { ... } }
{ "ok": false, "error": "..." }
```

**Discover all tools:**
```bash
curl -s http://100.120.53.61:4242/call | jq '.tools[].name'
```

---

## 3. Complete Tool Reference

#### New & Updated Capabilities (Latest)
- **New MCP Tools:** `xb_job_kill`, `xb_job_kill_all`, `xb_start_recording`, `xb_stop_recording`, `xb_runtime_config`, `xb_runtime_disconnect`, `xb_runtime_spawn`, `xb_save_contexts`, `xb_exit_node_set`, `xb_shell`, `xb_log_tail`.
- **Storage Migration (Phase 3):** Supabase fully replaced by **Cloudflare D1 + R2**. Python client: `colab/d1.py`. JS client: `src/core/d1.mjs`.
- **New D1 Tables:** `accounts`, `sessions`, `session_credentials`, `node_secrets`, `runtime_config`, `locks`, `node_pubkeys`, `node_registry`.
- **boot.py:** Injects `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID` into Node.js process env. TS state save/restore via R2.
- **Self-Spawn Params:** `tier` and `selection` added to `selectSpawnAccount`.
- **sync.py:** D1-first for all session state pull/push. TS state synced to R2.
- **session-manager.mjs:** Added `withSaveLock` mutex and improved `_isSafeGoogleCookieUpdate`.
- **browser-pool.mjs:** Added cached launch/context Promises and CDP session cleanup.

### 3.1 Workflow Execution

#### `xb_run_workflow` — Launch a workflow as a background job
```bash
curl -s -X POST http://100.120.53.61:4242/call \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "xb_run_workflow",
    "args": {
      "workflow":   "google-signin",
      "session_id": "user@gmail.com",
      "params":     { "email": "user@gmail.com", "password": "...", "totp_secret": "..." }
    }
  }' | jq '.result.job_id'
```
**Returns:** `job_id` (format: `{workflow-slug}_{YYYYMMDD}_{HHMMSS}_{8hex}`). Poll with `xb_job_poll`.

**Built-in workflows:**

| ID | Purpose | Required Params |
|----|---------|----------------|
| `self-spawn` | Opens `start.ipynb` in new Colab runtime → boots worker node | `notebook_file_id?`, `parent_job_id?` |
| `google-signin` | Logs into Google via uc stealth engine + CDP TOTP 2FA | `email`, `password`, `totp_secret` |
| `google-session-refresh` | Refreshes expiring Google session cookies | _(none)_ |
| `tailscale-signin` | Signs into Tailscale via Google OAuth (mesh-admin account) | `parent_job_id?` |
| `tailscale-auth` | Authorizes a new Tailscale node after `tailscale-signin` | _(none)_ |
| `tailscale-ssh-auth` | Clicks Tailscale auth URL to authorize new node | `auth_url` |
| `v0-signin` | Logs into v0.dev | `email`, `password` |
| `v0-signin-with-google` | Signs into v0.dev via Google OAuth | _(active Google session required)_ |
| `v0-export-session` | Exports v0 session cookies to Drive | _(none)_ |

---

#### `xb_job_poll` / `xb_job_list` / `xb_job_cancel` / `xb_job_pause` / `xb_job_resume` / `xb_job_paused_list`
```bash
# Poll status
curl -s -X POST http://100.120.53.61:4242/call \
  -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_poll","args":{"job_id":"google-signin_20260802_011001_db0da990"}}' \
  | jq '{status: .result.status, steps: [.result.steps[] | {name:.step_name, status:.status}]}'

# List (filter by status)
curl -s -X POST http://100.120.53.61:4242/call \
  -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_list","args":{"limit":10,"status":"running"}}' | jq '.result'

# Cancel / Pause / Resume / List paused
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_cancel","args":{"job_id":"<JOB_ID>"}}' | jq '.result'

curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_pause","args":{"job_id":"<JOB_ID>","reason":"Fixing issue"}}' | jq '.result'

curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_resume","args":{"job_id":"<JOB_ID>"}}' | jq '.result'

curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_paused_list","args":{}}' | jq '.result'
```

---

### 3.2 Hot-Patching (Fix Without Restart)

**Lifecycle:** `xb_patch` (write + cache-bust) → test → `xb_commit_patch` (git commit + push).
**Never commit speculatively.** Only commit after a successful run.

```bash
# Apply patch (busts ESM module cache automatically)
jq -n \
  --arg rel  "workflows/self-spawn.mjs" \
  --arg body "$(cat /path/to/fixed.mjs)" \
  --arg desc "Fix OAuth popup timeout" \
  '{"tool":"xb_patch","args":{"rel_path":$rel,"content":$body,"description":$desc}}' \
  | curl -s -X POST http://100.120.53.61:4242/call \
      -H "Content-Type: application/json" -d @- | jq '.result'

# Rollback (omit rel_path to rollback ALL)
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_patch_rollback","args":{"rel_path":"workflows/self-spawn.mjs"}}' | jq '.result'

# Commit pending patches to git + push
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_commit_patch","args":{"message":"fix: timeout","push":true}}' | jq '.result'

# Drive-only override (no git — fastest for rapid iteration)
jq -n --arg id "self-spawn" --arg src "$(cat wf.mjs)" \
  '{"tool":"xb_workflow_push","args":{"id":$id,"source":$src}}' \
  | curl -s -X POST http://100.120.53.61:4242/call \
      -H "Content-Type: application/json" -d @- | jq '.result'
```
⚠️ If server restarts between `xb_patch` and `xb_commit_patch`, the in-memory ledger is lost — re-apply patches.

---

### 3.3 Session Management

A **session** = one persistent Chrome profile + v2 JSON file holding cookies/localStorage/IndexedDB.

**Session ID:** Full Google email OR bare slug (e.g. `shahid.raiganj`). D1 stores the slug with **dots preserved**. Alias suffix (`+alias`) is stripped.

**Hybrid DB architecture:**
| Store | Contents | Scope |
|-------|----------|-------|
| **D1** `accounts` | email, tier, password, totp_secret, notes, is_active | Cross-runtime, permanent |
| **D1** `sessions` | id (slug), serial, account_email, exit_node, exit_node_set_at, is_persisted, cookie_state, last_seen_at | Cross-runtime, permanent |
| **D1** `session_credentials` | session_id, service, is_valid, failure_count, checked_by, last_checked, last_saved_at, node_name, metadata | Cross-runtime, permanent |
| **D1** `node_secrets` | key, value (r2_endpoint, r2_access_key_id, r2_secret_access_key, r2_bucket, cf_api_token, cf_account_id, cf_d1_database_id) | Cross-runtime, permanent |
| **D1** `runtime_config` | key, value, scope (composite PK: key+scope) | Cross-runtime, permanent |
| **D1** `locks` | key, node, acquired_at (REAL), expires_at (REAL unix epoch) | Ephemeral |
| **D1** `node_pubkeys` | node_name, pubkey, updated_at | Permanent |
| **D1** `node_registry` | node_id (PK=worker_id), node_name, ts_ip, port, mcp_url, git_head, status, last_seen | Ephemeral |
| **Local SQLite** `xio-browser.db` | jobs, drive_assets | Ephemeral per runtime |

> At startup `mcp-server.mjs` loads all D1 rows into in-memory Maps. All reads (`getAccount()`, `getSession()`, `listSessions()`) hit the Maps (zero latency). Writes go to Map AND D1 async.

**Canonical file naming (PRFL-NNN pattern):**
```
shahid.raiganj@gmail.com  →  serial 003  →  PRFL-003_shahid_raiganj
karmareturnsfromallsides@gmail.com  →  PRFL-001_karmareturnsfromallsides
```
- Drive (cold backup): `sessions/PRFL-003_shahid_raiganj.json`, `chrome_profiles/PRFL-003_shahid_raiganj.tar.gz`
- R2 (primary for profiles): `chrome_profiles/PRFL-003_shahid_raiganj.tar.gz` (bucket: `xio-mesh`)
- D1 (primary for cookies): `sessions` row `shahid_raiganj`, column `cookie_state TEXT (JSON)`

**Session tiers:** `"Pro"` (can spawn workers) | `"Starter"` (sign-in workflows only)

**v2 session JSON format:**
```json
{
  "_version": 2,
  "_captured_at": "2026-08-06T05:14:17Z",
  "_capture_method": "cdp_full",
  "_domains": [".google.com", ".accounts.google.com"],
  "cookies": [{"name":"SID","value":"...","domain":".google.com","path":"/","expires":1790000000,"httpOnly":false,"secure":true,"sameSite":"None","sourceScheme":"Secure","sourcePort":443}],
  "origins": [{"origin":"https://accounts.google.com","localStorage":[{"name":"k","value":"v"}]}],
  "indexedDB": [{"origin":"https://accounts.google.com","databases":[...]}]
}
```

```bash
# List all sessions
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_list","args":{}}' | jq '.result[]'

# Create slot
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_create","args":{"id":"user@gmail.com","tier":"Pro"}}' | jq '.result'

# ⚠️ IRREVERSIBLE — removes DB row + JSON + Drive tarball
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_delete","args":{"session_id":"user@gmail.com"}}' | jq '.result'

# Cookie expiry urgency (none | scheduled | soon | immediate)
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_health","args":{"session_id":"user@gmail.com"}}' | jq '.result'

# Live-verify login — READ-ONLY, does NOT write to session file
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_status","args":{"session_id":"user@gmail.com","service":"google"}}' | jq '.result'

# Manually mark service valid in D1 session_credentials
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_service_upsert","args":{"session_id":"user@gmail.com","service":"google","account_hint":"user@gmail.com","is_valid":true}}' | jq '.result'

# Export / Import between nodes
COOKIES=$(curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_export","args":{"session_id":"user@gmail.com"}}' | jq '.result.state')
curl -s -X POST http://100.120.53.62:4242/call -H "Content-Type: application/json" \
  -d "{\"tool\":\"xb_session_import\",\"args\":{\"session_id\":\"user@gmail.com\",\"state\":$COOKIES}}" | jq '.result'
```

---

### 3.4 Browser Interaction (Direct)

```bash
# Screenshot
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_interact","args":{"session_id":"user@gmail.com","action":"screenshot"}}' \
  | jq -r '.result.screenshot' | sed 's/data:image\/jpeg;base64,//' | base64 --decode > /tmp/sc.jpg

# Navigate / Click / Type / Evaluate
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_interact","args":{"session_id":"user@gmail.com","action":"navigate","url":"https://google.com"}}' | jq '.result'
```

---

### 3.5 DevTools (CDP) Tools

| Tool | Purpose | Key Args |
|------|---------|----------|
| `xb_devtools_screenshot` | Screenshot via CDP | `session_id?`, `format?`, `quality?` |
| `xb_devtools_evaluate` | Run JS in page | `expression`, `session_id?` |
| `xb_devtools_click` | Click at pixel coords | `x`, `y`, `session_id?` |
| `xb_devtools_type` | Type text char-by-char | `text`, `delay_ms?` |
| `xb_devtools_dom` | Full page HTML | `max_bytes?` (default 200 KB) |
| `xb_devtools_cookies` | Page cookies | `session_id?` |
| `xb_devtools_network_log` | Last 300 network requests | `filter?` (URL substring) |
| `xb_devtools_console_log` | Browser console messages | `level?`, `last?` |
| `xb_devtools_command` | Any raw CDP command | `method`, `params?` |
| `xb_devtools_url` | CDP WebSocket URL for Mac DevTools | `session_id?` |

```bash
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_devtools_command","args":{"method":"Network.getAllCookies","params":{}}}' \
  | jq '.result.result.cookies[] | select(.domain | contains("google"))'
```

---

### 3.6 Infrastructure Control

```bash
# Full restart with git pull
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_restart","args":{"pull_code":true}}' | jq '.result'

# Wait for restart
until curl -s --max-time 5 http://100.120.53.61:4242/health | jq -e '.ok' > /dev/null 2>&1; do
  sleep 3; echo "waiting..."
done && echo "Server back online!"

# Sync from Drive (what: "all" | "workflows" | "sessions" | "db")
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_sync","args":{"what":"all"}}' | jq '.result'

# Run shell command
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_shell","args":{"cmd":"tailscale status && df -h /content"}}' | jq -r '.result.stdout'

# Tail server logs (services: "xiobr" | "xiov0" | "tailscaled" | "syslog")
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_log_tail","args":{"service":"xiobr","lines":50}}' | jq -r '.result.lines[]'

# Full mesh topology
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_node_status","args":{}}' | jq '.result'

# Change exit node (only affects NEW unbound sessions)
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_exit_node_set","args":{"peer_ip":"100.64.x.x"}}' | jq '.result'

# Graceful shutdown
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_shutdown","args":{"reason":"Maintenance"}}' | jq '.result'
```

`xb_restart` args: `pull_code` (default true), `delay_ms` (default 800), `preserve_sessions` (default false), `pause_jobs` (default false)

---

## 4. File System Layout on Colab

```
/tmp/
  xiobr.log              <- MCP server stdout
  restart.log            <- restart.py output
  xio_config.json        <- Runtime config (cf_api_token, cf_account_id, cf_d1_database_id, etc.)

/content/
  xio-browser/           <- Git repo — MDShahid94/xio-browser
    bin/xio-browser.mjs  <- CLI entry point
    src/core/
      db.mjs             <- SQLite schema + D1 helpers (sessions, jobs, accounts, locks)
      d1.mjs             <- Cloudflare D1 HTTP client (select, upsert, update, acquireLock, etc.)
      session-manager.mjs, browser-pool.mjs, stealth-runner.mjs, ...
    workflows/           <- .mjs workflow plugins
    colab/
      boot.py            <- Full startup: auth -> node detection -> D1 secrets -> TS -> MCP
      sync.py            <- Drive/D1/R2 <-> local sync
      d1.py              <- Python D1 HTTP client
      registry.py        <- Node registry (D1-backed)
      spawner.py         <- Self-spawn logic (D1-backed)
      start.ipynb        <- Operator config (Cell 1=CF credentials + config, Cell 2=boot)

  xio-mesh/              <- Google Drive mount (cold backup + ephemeral jobs)
    chrome_profiles/     <- Chrome profile cold backup (primary is R2)
    sessions/            <- v2 session file cold backup (primary is D1)
    xio_config/          <- Runtime config overrides (Drive JSON files)
    lock/                <- Drive locks (google_signin, ts state)
    tailscale_states/    <- TS identity cold backup (primary is R2 ts_states/)
    jobs/                <- Job artifacts (result.json, final.jpg; steps/ local only)
    xio-browser.db       <- SQLite DB snapshot
```

**Cloudflare R2 `xio-mesh` bucket layout:**
```
chrome_profiles/          <- Primary Chrome profile store
  PRFL-NNN_<username>.tar.gz
ts_states/                <- Primary Tailscale identity store (binary)
  TS_colab-master.state
  TS_colab-worker-<u>.state
cache/                    <- Optional Drive-bypassing warm-start cache
db/                       <- Periodic SQLite snapshot (every 10 min via keep-alive)
  xio-browser.db
```

---

## 5. Session v2 Persistence — How It Works

### 5.1 Capture
`saveStorageStateFull(sessionId, page)` uses CDP to capture: all cookies (full field preservation), localStorage, IndexedDB. Merges with existing file — longer-TTL cookies win.

### 5.2 Anti-Degradation
`saveStorageState()` merge-saves after every job: keeps whichever cookie has the longer TTL.

### 5.3 Restore
`browser-pool.getContext()` → `loadAndHydrateContext()`: injects cookies + localStorage + IndexedDB + warm-up navigations.

### 5.4 Save Triggers

| Event | Push destination |
|-------|-----------------|
| `google-signin` Phase 3 login | **D1** `sessions.cookie_state` immediately + Drive (cold) |
| Any workflow completes | **D1** immediately + Drive |
| `xb_shutdown` / `xb_restart` | `sync.py --push sessions` (D1 + Drive) |
| Keep-alive tick (every 60s) | `sync.py --push --what sessions --target d1` |
| Keep-alive tick (every 5 min) | `sync.py --push --what db` (Drive + R2) |
| TS state watcher | R2 `ts_states/TS_{node}.state` within 30s + Drive cold backup |
| Chrome profile (google-signin Phase 3) | **R2** immediately (~2s); Drive in background |

### 5.5 Config (start.ipynb Cell 1)
```python
# Cloudflare credentials (required)
CF_API_TOKEN      = 'cfat_...'
CF_ACCOUNT_ID     = 'e63a13ef...'
CF_D1_DATABASE_ID = 'acfd72de-...'

INDEXEDDB_ORIGINS = ["https://accounts.google.com","https://myaccount.google.com","https://www.google.com","https://v0.dev"]
LOCALSTORAGE_MIN_COOKIES = 5
WARMUP_ORIGINS   = ["https://accounts.google.com","https://myaccount.google.com"]
AUTO_SPAWN_ENABLED       = True
AUTO_SPAWN_AFTER_MINUTES = 12
AUTO_SPAWN_GOOGLE_SIGNIN = 'auto'  # 'auto'|'skip'|'force'
JOB_RETENTION_DAYS = 3

DEVICE_FINGERPRINT = [
    {'cores':10,'ram':16,'webgl_renderer':'Apple M4','macos_version':'15.2.0','width':2560,'height':1440},
    {'cores':8,'ram':8,'webgl_renderer':'Apple M2','macos_version':'14.6.1','width':1920,'height':1200},
]
```

---

## 6. Node Identity Detection

```
boot.py:
  1. _fetch_runtime_email() -> GET oauth2.googleapis.com/tokeninfo
  2. _fetch_account_emails_from_drive() -> D1: SELECT email FROM accounts
  3. runtime_email IN accounts?
       YES -> NODE_NAME = colab-worker-<slug>   is_worker = True
       NO  -> NODE_NAME = colab-master           is_worker = False
  4. Writes to /tmp/xio_config.json
```

**Slug formula:**
```python
email.split('@')[0].split('+')[0].replace('.', '-')
# shahid.raiganj@gmail.com  ->  colab-worker-shahid-raiganj
```

**Fallback:** D1 unreachable → defaults to `colab-master` ✅

| Scenario | NODE_NAME | R2 TS state key |
|---------|-----------|-----------------|
| Owner's email (not in D1 accounts) | `colab-master` | `ts_states/TS_colab-master.state` |
| `shahid.raiganj@gmail.com` (in D1) | `colab-worker-shahid-raiganj` | `ts_states/TS_colab-worker-shahid-raiganj.state` |

### Adding a worker account
1. `xb_session_create` with the worker Gmail
2. Run `google-signin` workflow → populates D1 `accounts` table
3. Next boot of that Gmail's runtime → auto-detected as worker

---

## 7. Standard Agent Workflows

### 7.1 Start-of-Session Checklist
```bash
curl -s http://100.120.53.61:4242/health | jq '{ok, version, running_job}'
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_paused_list","args":{}}' | jq '.result'
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_list","args":{"limit":5}}' | jq '.result[]|{id:.id,status:.status}'
```

### 7.2 Login New Google Account
```bash
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_session_create","args":{"id":"newuser@gmail.com","tier":"Starter"}}' | jq '.result'

JOB_ID=$(curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_run_workflow","args":{"workflow":"google-signin","session_id":"newuser@gmail.com","params":{"email":"newuser@gmail.com","password":"...","totp_secret":"..."}}}' \
  | jq -r '.result.job_id')

while true; do
  STATUS=$(curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
    -d "{\"tool\":\"xb_job_poll\",\"args\":{\"job_id\":\"$JOB_ID\"}}" | jq -r '.result.status')
  echo "Status: $STATUS"; [[ "$STATUS" =~ ^(done|error|cancelled|paused)$ ]] && break; sleep 10
done
```

### 7.3 After Code Commits on Mac
```bash
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_restart","args":{"pull_code":true}}' | jq '.result'
until curl -s --max-time 5 http://100.120.53.61:4242/health | jq -e '.ok' > /dev/null 2>&1; do
  sleep 3; echo "waiting..."
done && echo "Server back online!"
```

### 7.4 Fix-While-Running (Pause -> Patch -> Resume)
```bash
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d "{\"tool\":\"xb_job_pause\",\"args\":{\"job_id\":\"$JOB_ID\",\"reason\":\"Fixing issue\"}}" | jq '.result'

jq -n --arg rel "workflows/self-spawn.mjs" --arg src "$(cat fixed.mjs)" --arg desc "Fix timeout" \
  '{"tool":"xb_patch","args":{"rel_path":$rel,"content":$src,"description":$desc}}' \
  | curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" -d @- | jq '.result'

curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d "{\"tool\":\"xb_job_resume\",\"args\":{\"job_id\":\"$JOB_ID\"}}" | jq '.result'

# Only after successful run — commit to git
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_commit_patch","args":{"message":"fix: timeout","push":true}}' | jq '.result'
```

### 7.5 Session Expired During a Job
1. **DO NOT** delete the Chrome profile — permanent and irreversible.
2. Run `google-signin` workflow → saves fresh v2 cookies.
3. Resume or re-run original workflow.

> [!NOTE] `AUTO_SPAWN_GOOGLE_SIGNIN='skip'` makes self-spawn use existing session as-is without re-login.

### 7.6 Writing Sub-Workflows with `ctx.runInline()`
```js
// Always use ctx.runInline() — same process, no queue, no deadlock
const r = await ctx.runInline('google-signin', sessionId);
if (!r.ok) ctx.log(`google-signin failed: ${r.error}`);

const r2 = await ctx.runInline('tailscale-auth', sessionId, { ts_url: authUrl });
```

**Wrong (deadlocks inside a running workflow):**
```js
// ❌ Never do this — deadlocks job queue
const r = await fetch('http://localhost:4242/call', { method: 'POST', body: JSON.stringify({tool:'xb_run_workflow',...}) });
```

---

## 8. Live Monitoring URLs

| URL | Purpose |
|-----|---------|
| `http://<IP>:4242/health` | Liveness: `{ok, version, port, running_job}` |
| `http://<IP>:4242/call` | Tool registry (GET) + execution (POST) |
| `http://<IP>:4242/events` | SSE job lifecycle stream |
| `http://<IP>:4242/stream` | MJPEG live browser view |
| `http://<IP>:4242/stream/sse/view` | HTML viewer page |
| `http://<IP>:4242/jobs/<job_id>` | List all files for a job |
| `http://<IP>:9222` | Chrome DevTools remote debugging |

---

## 9. Important Code Patterns

### 9.1 Shadow DOM Click (Colab Dialogs)
```javascript
await page.evaluate(() => {
  function findInShadow(root, predicate) {
    if (!root) return null;
    for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
      if (predicate(el)) return el;
      if (el.shadowRoot) { const f = findInShadow(el.shadowRoot, predicate); if (f) return f; }
    }
    return null;
  }
  const btn = findInShadow(document, el =>
    ['button','mwc-button','paper-button'].includes(el.tagName?.toLowerCase()??'') &&
    /run\s*anyway|^allow$/i.test((el.textContent??'').trim())
  );
  if (btn) { btn.click(); return btn.textContent?.trim(); }
  return null;
});
```

### 9.2 Tailscale Peer Detection
```javascript
const { execSync } = await import('child_process');
const status = JSON.parse(execSync('tailscale status --json', { encoding: 'utf8', timeout: 8000 }));
const newPeer = Object.values(status.Peer ?? {}).find(p =>
  p.Online && !knownIPs.has(p.TailscaleIPs?.[0])
);
if (newPeer) ctx.log(`New worker online at ${newPeer.TailscaleIPs.find(ip=>ip.startsWith('100.'))} (${newPeer.HostName})`);
```

### 9.3 Port Resolution (Never Hardcode 4242)
```bash
PORT=$(python3 -c "import json; c=json.load(open('/tmp/xio_config.json')); print(c.get('xiobr_port',4242))" 2>/dev/null || echo 4242)
```

### 9.4 Standard Screenshot Pattern — `wf-shot.mjs`
```js
import { createShot } from '../src/core/wf-shot.mjs';
const shot = createShot(`${_jobDir}/steps`, { logFn: ctx.log.bind(ctx) });
shot.setPage(tsPage);
await shot('initial');            // → 01_initial.jpg
await shot('login_page');         // → 02_login_page.jpg
await shot('error', {png:true});  // → 03_error.png
if (shot.lastPath) fs.copyFileSync(shot.lastPath, `${_jobDir}/result_final.jpg`);
```

**Shot label conventions:**

| Moment | Label |
|--------|-------|
| Initial page load | `initial` |
| After navigation | `<page>_loaded` |
| After key click | `after_<action>` |
| Before key click | `<button>_visible` |
| Verified state | `<phase>_SUCCESS` |
| Error state | `<phase>_FAILED` |
| HITL screen | `device_verify` |
| OAuth screens | `oauth_screen_A`, `oauth_screen_B`, `oauth_done` |
| Poll snapshot | `poll_<elapsed>s` |
| Session result | `session_valid` or `session_invalid` |

### 9.5 HITL Auto-Pause Flow
```
1. Workflow detects HITL → ctx.hitl(message, {instructions:'tap 78'})
2. Screenshot → steps/HITL_<ts>.jpg
3. steps/hitl_notice.json written
4. SSE event emitted: job.hitl {job_id, message, shot_path, instructions}
5. Agent wakes, reads notice, tells user
6. User acts → agent calls xb_job_resume
```

```bash
curl -s http://100.120.53.61:4242/jobs/<JOB_ID>/steps/hitl_notice.json | jq .
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_job_resume","args":{"job_id":"<JOB_ID>"}}' | jq '.result'
```

---

## 10. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `/health` -> connection refused | MCP server crashed | `xb_shell` -> `cat /tmp/xiobr.log \| tail -40`; restart via Colab UI if dead |
| `verify_session` fails "expired" | Stale cookies after runtime reset | Run `google-signin` workflow — v2 JSON + warm-up auto-renews |
| Worker detected as colab-master | Email not in D1 accounts table | Run `google-signin` for worker account to populate D1 |
| R2 push fails (NoSuchBucket) | Old bucket name `xio-profiles` referenced | Bucket is `xio-mesh`. Update `r2_bucket` in D1 `node_secrets` |
| D1 queries returning 401 | Wrong CF credentials | Check `cf_api_token`, `cf_account_id`, `cf_d1_database_id` in Cell 1 |
| Job stuck `running` >30 min | Browser hung or TS auth pending | Check `/stream/sse/view`; use `xb_devtools_screenshot` |
| Worker not in Tailscale mesh | Auth URL not clicked | Check `steps/` screenshots; run `tailscale-ssh-auth` |
| `spawn_lock.json` stuck | Spawn enqueue failed | Boot clears locks >6min old; or `xb_shell rm /content/xio-mesh/registry/spawn_lock.json` |
| `TS_colab-master.state.__lock` conflict | Two runtimes sharing same identity | `--unlock-all` on stale node; restart new one |
| Hot-patch not taking effect | Module cache not busted | Use `xb_patch` (auto cache-busts) — NOT manual file write |
| `self-spawn` step 8 timeout | Slow Drive sync on first worker boot | Normal; check `steps/` evidence |
| `tailscale-signin` device verify HITL | Google "Verify it's you" number prompt | Job auto-pauses. Read `steps/hitl_notice.json`. Complete on device, then `xb_job_resume` |
| Session `google-signin` HITL | `data-challengetype 14/16` — no "Try another way" | stealth-runner triggers `ctx.hitl()`. Complete on device, `xb_job_resume` |
| Chrome profile deleted on session expiry | Old `evictSession()` called `cleanLocalProfile()` | Fixed: evictSession only deletes JSON + resets is_persisted; profile dir always preserved |
| Cookie loss on browser crash | disconnect handler cleared contexts | Fixed: emergency snapshot of all contexts before clearing |

---

## 11. Database Schema

> [!IMPORTANT]
> These schemas are **verified from `PRAGMA table_info()`** on the live D1 database (2026-08-18).
> Do NOT infer schema from code comments — always verify with `PRAGMA table_info(<table>)` if in doubt.

### 11.1 Cloudflare D1 — `xio-mesh` (permanent, cross-runtime)

```sql
-- Verified 2026-08-18 via PRAGMA table_info()

CREATE TABLE accounts (
  email       TEXT PRIMARY KEY,
  tier        TEXT NOT NULL DEFAULT 'Starter',
  password    TEXT NOT NULL,           -- required (not nullable)
  totp_secret TEXT,
  notes       TEXT,                    -- freeform metadata
  is_active   INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT datetime('now')
);
-- ⚠️  No 'display_name' column (removed). password is NOT NULL (differs from old docs).

CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,    -- slug: 'shahid_raiganj'
  serial          INTEGER NOT NULL,    -- stable PRFL-NNN serial (NOT NULL, NOT UNIQUE)
  account_email   TEXT,                -- references accounts(email)
  exit_node       TEXT,
  exit_node_set_at TEXT,               -- timestamp when exit_node was last changed
  is_persisted    INTEGER NOT NULL DEFAULT 0,
  cookie_state    TEXT,                -- v2 session JSON (primary cookie store)
  state_pushed_at TEXT,
  last_seen_at    TEXT,
  notes           TEXT,
  created_at      TEXT NOT NULL DEFAULT datetime('now')
);
-- ⚠️  No 'tier' column (use accounts.tier via JOIN).
-- ⚠️  No 'bound_at' column (replaced by exit_node_set_at).
-- ⚠️  serial is NOT UNIQUE (changed from old schema).

CREATE TABLE session_credentials (
  session_id    TEXT NOT NULL,         -- PK part 1
  service       TEXT NOT NULL,         -- PK part 2: 'google'|'v0'|'tailscale'|etc.
  account_hint  TEXT,
  is_valid      INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  checked_by    TEXT,                  -- node_name of last checker
  last_checked  TEXT,
  last_saved_at TEXT,                  -- when cookie_state was last written
  node_name     TEXT,
  metadata      TEXT,                  -- JSON
  PRIMARY KEY (session_id, service)
);
-- ⚠️  Has 'checked_by' and 'last_saved_at' (not in old docs).

CREATE TABLE node_secrets (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
  -- NO created_at column (differs from old docs)
  -- keys: r2_endpoint, r2_access_key_id, r2_secret_access_key,
  --       r2_bucket, cf_api_token, cf_account_id, cf_d1_database_id
);

CREATE TABLE runtime_config (
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  scope      TEXT NOT NULL DEFAULT 'global',  -- composite PK with key
  updated_at TEXT DEFAULT datetime('now'),    -- NOT NULL dropped vs old docs
  PRIMARY KEY (key, scope)
);
-- ⚠️  Composite PK (key, scope) — NOT just key alone.

CREATE TABLE locks (
  key         TEXT PRIMARY KEY,        -- e.g. 'tailscale_states/TS_colab-master.state'
  node        TEXT NOT NULL,
  acquired_at REAL NOT NULL,           -- Unix epoch (REAL, not TEXT)
  expires_at  REAL NOT NULL            -- Unix epoch (REAL, not TEXT)
);

CREATE TABLE node_pubkeys (
  node_name  TEXT PRIMARY KEY,
  pubkey     TEXT NOT NULL,
  updated_at TEXT DEFAULT datetime('now')  -- nullable (no NOT NULL)
);

CREATE TABLE node_registry (
  node_id   TEXT PRIMARY KEY,          -- worker_id: 'colab_20260818_171203_ed82'
  node_name TEXT NOT NULL,             -- 'colab-master' | 'colab-worker-shahid-raiganj'
  ts_ip     TEXT,
  port      INTEGER NOT NULL DEFAULT 4242,
  mcp_url   TEXT,
  git_head  TEXT,                      -- short SHA of deployed commit
  status    TEXT NOT NULL DEFAULT 'alive',  -- 'alive' | 'retired'
  last_seen TEXT DEFAULT datetime('now')
);
-- ⚠️  node_id (NOT node_name) is the PK.
-- ⚠️  Has port, git_head, status columns (absent from old docs).
-- ⚠️  No 'version' or 'mcp_url'-only schema — full 8-column table.
-- Worker considered alive if: status='alive' AND last_seen > datetime('now','-90 seconds')
```

### 11.2 Local SQLite — `xio-browser.db` (ephemeral per runtime)

```sql
CREATE TABLE jobs (
  id         TEXT PRIMARY KEY,        -- {workflow}_{YYYYMMDD}_{HHMMSS}_{8hex}
  workflow   TEXT NOT NULL,
  session_id TEXT,
  status     TEXT NOT NULL,           -- pending|running|done|error|cancelled|paused
  params     TEXT,                    -- JSON
  result     TEXT,                    -- JSON
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);

CREATE TABLE drive_assets (
  id         TEXT PRIMARY KEY,
  local_path TEXT,
  drive_path TEXT,
  pushed_at  TEXT
);
```

---

## 12. Environment Variables & Config

| Variable | Default | Purpose |
|----------|---------|---------| 
| `CHROME_PROFILES_DIR` | `/content/xio-mesh/chrome_profiles` | Override Chrome profile base dir |
| `XIOBR_PORT` | `4242` | Override HTTP port |
| `XIO_NODE_NAME` | `colab-master` | Node name — injected by boot.py via `XIOBR_CMD` |
| `CF_API_TOKEN` | _(from Cell 1)_ | Cloudflare API token — injected by boot.py |
| `CF_ACCOUNT_ID` | _(from Cell 1)_ | Cloudflare account ID — injected by boot.py |
| `CF_D1_DATABASE_ID` | _(from Cell 1)_ | D1 database UUID — injected by boot.py |
| `XIO_IS_WORKER` | _(unset)_ | Legacy (kept for backward compat — detection now uses D1) |

**Primary configuration surface: `start.ipynb` Cell 1** — values flow through `/tmp/xio_config.json`.

---

## 13. Runtime Configuration & Management

### Drive-backed Config (runtime overrides)

- `xio_config/global.json` — applies to ALL nodes
- `xio_config/{NODE_NAME}.json` — applies to one specific node (highest priority)

**Priority:** per-node Drive → global Drive → D1 `runtime_config` → Cell 1 defaults

### D1 `runtime_config` table (infrastructure metrics)

```
key = 'r2_usage_last_check'
value = {"used_gb":5.1,"class_a_mtd":12450,"class_b_mtd":98230,"ts":"2026-08-18T15:00:00Z"}
```

**R2 usage guard thresholds:**

| Threshold | Storage | Class A ops | Action |
|-----------|---------|-------------|--------|
| Soft (80%) | 8 GB | 800 K | ⚠️ warn + D1 alert |
| Hard (95%) | 9 GB | 950 K | 🚨 auto-delete oldest profiles |
| Free tier max | 10 GB | 1 M | Never reached if guard active |

### `xb_runtime_config`
```bash
# Get current effective config
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_runtime_config","args":{"action":"get"}}' | jq '.result.effective_config'

# Set for ALL nodes
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_runtime_config","args":{"action":"set","key":"auto_spawn_after_minutes","value":20,"scope":"global"}}' | jq '.result'

# Set for specific worker
curl -s -X POST http://100.120.53.61:4242/call -H "Content-Type: application/json" \
  -d '{"tool":"xb_runtime_config","args":{"action":"set","key":"default_exit","value":"100.x.x.x","scope":"node","node":"colab-worker-shahid-raiganj"}}' | jq '.result'
```

### Profile Isolation for Concurrent Runtimes

1. **Drive lock** (`lock/google_signin_{slug}`) — prevents concurrent `google-signin` for same account
2. **Node-specific Chrome profile dir** — `restoreProfile()` extracts to `PRFL-NNN_slug__colab-worker-xyz/`
3. **D1 distributed locks** — `acquireSessionLock(sessionId, nodeName)` in `db.mjs` (TTL-based, auto-expires)

(Tool count: 47 total tools)
