// ─── MCP Server — All Tool Definitions ────────────────────────────────────
// Tools:
//   Workflow Execution : xb_run_workflow, xb_job_poll, xb_job_cancel, xb_job_list, xb_interact
//   Job Control        : xb_job_pause, xb_job_resume, xb_job_paused_list
//   Hot-Patch          : xb_patch, xb_patch_rollback, xb_commit_patch
//   Session Mgmt      : xb_session_list, xb_session_create, xb_session_delete,
//                       xb_session_status, xb_session_export, xb_session_import,
//                       xb_session_health
//   Workflow Plugins   : xb_workflow_list, xb_workflow_get, xb_workflow_push, xb_workflow_delete
//   Server            : xb_node_status, xb_sync, xb_restart, xb_shutdown, xb_shell, xb_log_tail,
//                       xb_d1_query, xb_r2_list
//   DevTools (CDP)    : xb_devtools_screenshot, xb_devtools_evaluate, xb_devtools_click,
//                       xb_devtools_type, xb_devtools_dom, xb_devtools_cookies,
//                       xb_devtools_network_log, xb_devtools_console_log, xb_devtools_command,
//                       xb_devtools_url

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { execSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { createLogger } from './utils/logger.mjs';
import { initDb, upsertSession, getSession, listSessions, deleteSession,
         upsertSessionService, listSessionServices, markServiceValid, getDb } from './core/db.mjs';
import { enqueueJob, pollJob, cancelJob, listAllJobs, getRunningJob, getRunningJobs,
         getPausedJob, listPausedJobs, resumePausedJob, forceResetRunner,
         getSlotCounts, getRuntimeType, getConcurrencyLimits } from './core/job-manager.mjs';
import { killSidecar }             from './core/stealth-runner.mjs';
import { sessionStatePath, loadStorageState, saveStorageState, deleteStorageState } from './core/session-manager.mjs';
import { getContext, getBrowserWSEndpoint } from './core/browser-pool.mjs';
import { listWorkflows, getWorkflowSource, writeWorkflow, deleteWorkflow, getWorkflowMeta,
         patchWorkflowFile, patchArbitraryFile } from './core/workflow-runner.mjs';
import { selfIP, getPeerStatus, setExitNode,
         setDefaultExitNode, getDefaultExitNode, listAvailablePeers,
         setDeviceSpecs } from './utils/tailscale.mjs';
import { setDriveRoot, ensureAllDirs } from './utils/drive.mjs';
import { getSessionExitBinding } from './core/db.mjs';
import {
  getCDPSession,
  cdpSend, cdpScreenshot, cdpEvaluate, cdpClick, cdpType, cdpGetDOM, cdpGetCookies,
  cdpGetAllCookies,
  getNetworkLog, getConsoleLog,
} from './core/devtools-manager.mjs';
import { checkCookieHealth, checkAllSessionsHealth } from './utils/cookie-health.mjs';
import { recorder } from './core/action-recorder.mjs';
import { normaliseSessionId } from './utils/session-id.mjs';
import { runtimeConfigTool, runtimeDisconnectTool, runtimeSpawnTool, saveContextsTool } from './tools/runtime-manager.mjs';

const log = createLogger('mcp-server');

// ── Server Factory ─────────────────────────────────────────────────────────

export async function createServer(config) {
  const { driveRoot, defaultExit, dbPath } = config;

  // Initialize Drive paths
  setDriveRoot(driveRoot);
  ensureAllDirs();

  // Initialize SQLite + sync from D1 into local in-memory Maps (must await)
  await initDb(dbPath);

  // Seed the runtime default exit node from config (can be overridden by xb_exit_node_set)
  if (defaultExit) setDefaultExitNode(defaultExit);

  // Inject real device specs from CONFIG.mac_specs (start.ipynb Cell 1 → boot.py → --device-specs)
  // These override hardcoded fingerprint fallbacks so Chrome looks like the real exit node hardware.
  if (config.deviceSpecs) setDeviceSpecs(config.deviceSpecs);

  const server = new McpServer({
    name:    'xio-browser',
    version: '0.5.0',
  });

  // ── Helper: wrap tool with error handling + register in /call registry ─────
  // Every registered tool is available via both POST /mcp (SSE-framed) and
  // POST /call (direct synchronous JSON). The registry is read by http-transport.
  if (!globalThis.__xioToolRegistry) globalThis.__xioToolRegistry = new Map();
  const _reg = globalThis.__xioToolRegistry;

  function tool(name, schema, handler, { description } = {}) {
    server.tool(name, schema, async (args) => {
      try {
        const result = await handler(args);
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      } catch (err) {
        log.error(`Tool ${name} error: ${err.message}`);
        return {
          content: [{ type: 'text', text: JSON.stringify({ error: err.message }) }],
          isError: true,
        };
      }
    });
    // Register directly-callable handler for POST /call
    _reg.set(name, { handler, description: description ?? null });
  }


  // ═══════════════════════════════════════════════════════════════════════
  // WORKFLOW EXECUTION TOOLS
  // ═══════════════════════════════════════════════════════════════════════

  tool('xb_run_workflow', {
    workflow:   z.string().describe('Workflow plugin ID (filename without .mjs)'),
    session_id: z.string().optional().describe('Session slot to use (for cookie persistence)'),
    params:     z.record(z.unknown()).optional().default({}).describe('Workflow-specific parameters (e.g. email, password)'),
    exit_node:  z.string().optional().describe(`Tailscale IP of exit node. Defaults to current default (${defaultExit}). Must match session's bound exit node if session has one.`),
    force:      z.boolean().optional().default(false).describe(
      'Override session↔exit-node binding mismatch. DANGEROUS: may trigger bot detection or account protection. Only use if you understand the risk.'
    ),
    logging:    z.boolean().optional().default(false).describe('Enable job step logging, screenshots, and Drive push. Off by default for zero overhead.'),
    streaming:  z.boolean().optional().default(false).describe('Enable SSE streaming of job lifecycle events. Off by default.'),
    slot_type:  z.enum(['headed', 'headless', 'appetize']).optional().describe('Browser slot type. Auto-detected if omitted.'),
  }, async ({ workflow, session_id, params, exit_node, force, logging, streaming, slot_type }) => {
    const exitNode   = exit_node ?? getDefaultExitNode() ?? defaultExit;
    const sessionId  = normaliseSessionId(session_id);

    const result = enqueueJob({
      workflowId: workflow,
      sessionId,
      exitNode,
      params:     params ?? {},
      force,
      enableLogging:   logging,
      enableStreaming:  streaming,
      slotType:        slot_type,
    });

    if (result && typeof result === 'object' && result.binding_error) {
      return result;
    }

    const jobId = result;
    const tsIP = selfIP();
    const port = config.httpPort ?? 4242;
    const streamUrl   = tsIP ? `http://${tsIP}:${port}/stream`   : null;
    const devtoolsUrl = tsIP ? `http://${tsIP}:${port}/devtools` : null;
    const eventsUrl   = tsIP ? `http://${tsIP}:${port}/events`   : null;
    const filesUrl    = tsIP ? `http://${tsIP}:${port}/jobs/${jobId}` : null;

    return {
      job_id:       jobId,
      status:       'running',
      exit_node:    exitNode,
      message:      `Workflow '${workflow}' started. Poll with xb_job_poll or subscribe to events_url.`,
      logging_enabled:   logging,
      streaming_enabled: streaming,
      stream_url:   streamUrl,
      devtools_url: devtoolsUrl,
      events_url:   eventsUrl,
      files_url:    filesUrl,
      open_stream:  streamUrl  ? `open "${streamUrl}"` : null,
    };
  }, { description: 'Run a named workflow plugin in the background browser. Returns a job_id immediately. Poll with xb_job_poll or subscribe to events_url for completion. Set logging:true and streaming:true to enable step tracking and SSE.' });

  tool('xb_job_poll', {
    job_id: z.string().describe('Job ID returned by xb_run_workflow'),
  }, async ({ job_id }) => {
    const result = pollJob(job_id);
    if (!result) return { error: `Job not found: ${job_id}` };
    return result;
  }, { description: 'Poll the status and result of a background workflow job. Returns status (pending|running|done|error|cancelled), logs, and result payload when done.' });

  tool('xb_job_cancel', {
    job_id: z.string().describe('Job ID to cancel'),
  }, async ({ job_id }) => {
    const ok = cancelJob(job_id);     // removes from queue or sets cancelling flag
    killSidecar(job_id);              // force-kill any running python/chrome process
    return { cancelled: ok, job_id };
  }, { description: 'Cancel a pending or running workflow job by its job_id. Force-kills any running chrome/python sidecar process.' });

  tool('xb_job_kill', {
    job_id:  z.string().describe('Job ID to force-kill'),
    signal:  z.enum(['SIGTERM', 'SIGKILL']).optional().default('SIGKILL')
              .describe('Signal to send (default SIGKILL — immediate, no cleanup)'),
  }, async ({ job_id, signal }) => {
    const sidecarKilled = killSidecar(job_id);
    const cancelled     = cancelJob(job_id);
    // Also try to kill any stray chrome/python by job marker in /proc
    let procKilled = 0;
    try {
      const { execSync } = await import('node:child_process');
      // Find pids that have the job_id in their cmdline
      const out = execSync(
        `grep -rl ${job_id} /proc/*/cmdline 2>/dev/null | awk -F/ '{print $3}'`,
        { encoding: 'utf8', timeout: 3000 }
      ).trim();
      for (const pid of out.split('\n').filter(Boolean)) {
        try { process.kill(parseInt(pid), signal); procKilled++; } catch (_) {}
      }
    } catch (_) {}
    return { job_id, sidecar_killed: sidecarKilled, queue_cancelled: cancelled, proc_killed: procKilled, signal };
  }, { description: 'Nuclear option: force-kill a stuck job\'s sidecar process + any stray chrome/python referencing the job_id. Use when xb_job_cancel is not enough.' });

  tool('xb_job_kill_all', {
    confirm: z.literal('yes').describe('Must be "yes" to confirm killing ALL running chrome/python sidecar processes'),
    signal:  z.enum(['SIGTERM', 'SIGKILL']).optional().default('SIGKILL')
              .describe('Signal to send (default SIGKILL)'),
  }, async ({ confirm: _c, signal }) => {
    if (_c !== 'yes') return { error: 'Confirmation required — pass confirm: "yes"' };
    const { execSync } = await import('node:child_process');
    let killed = [];
    // Kill all registered sidecars
    const { killSidecar: _ks } = await import('./core/stealth-runner.mjs');
    // Kill chrome/chromium/undetected-chromedriver
    for (const pattern of ['chrome', 'chromium', 'chromedriver', 'google_stealth']) {
      try {
        execSync(`pkill -${signal === 'SIGKILL' ? 9 : 15} -f ${pattern} 2>/dev/null || true`,
          { timeout: 3000 });
        killed.push(pattern);
      } catch (_) {}
    }
    // Mark any running job as cancelled
    const { listAllJobs: _lj, cancelJob: _cj } = await import('./core/job-manager.mjs');
    const running = _lj({ status: 'running', limit: 10 });
    for (const j of running) { try { _cj(j.id); } catch (_) {} }
    return {
      signal,
      patterns_killed: killed,
      jobs_cancelled: running.map(j => j.id),
      message: `Sent ${signal} to all chrome/python sidecar processes`,
    };
  }, { description: 'Emergency: kill ALL chrome/python sidecar processes across all jobs. Use when the server is frozen due to a stuck chrome instance. Requires confirm:"yes".' });

  tool('xb_job_list', {
    status: z.enum(['pending', 'running', 'done', 'error', 'cancelled', 'paused']).optional()
              .describe('Filter by status (omit for all)'),
    limit:  z.number().optional().default(20).describe('Max number of jobs to return'),
  }, async ({ status, limit }) => {
    const jobs = listAllJobs({ status, limit });
    // Annotate paused jobs with their checkpoint info
    const paused = listPausedJobs();
    const pausedMap = Object.fromEntries(paused.map(p => [p.job_id, p]));
    return jobs.map(j => pausedMap[j.id] ? { ...j, paused_info: pausedMap[j.id] } : j);
  }, { description: 'List recent workflow jobs. Status: pending|running|done|error|cancelled|paused. Paused jobs include paused_info with step/reason.' });

  tool('xb_force_reset_runner', {
    timeout_ms: z.number().optional().default(6000)
                  .describe('Grace period (ms) to wait for _running to clear before force-resetting (default 6000)'),
  }, async ({ timeout_ms }) => {
    // Emergency escape hatch — use after HITL release when the job runner is stuck.
    // Root cause: a paused job keeps _running=true forever because the finally block
    // only runs when the job finishes. After HITL pause/resume, if the resolve is lost,
    // the runner is permanently blocked.
    //
    // This tool:
    //   1. Cancels + auto-resumes all paused jobs (cancelJob now auto-resumes paused jobs)
    //   2. Waits up to timeout_ms for _running to clear naturally
    //   3. If still stuck, force-sets _running=false and kicks the queue
    // Sessions are 100% safe — this never touches the browser context.
    const result = await forceResetRunner(timeout_ms);
    return { ok: true, ...result, message: result.force_reset
      ? '⚠️ _running was force-reset (job was deeply stuck). Sessions unaffected.'
      : '✅ Job runner released cleanly. Pending jobs will now start.' };
  }, { description: 'Emergency: reset a stuck job runner. Safely releases all paused jobs, waits for pool to drain, force-resets if needed. Never touches the browser/sessions.' });

  tool('xb_pool_status', {}, async () => {
    return getSlotCounts();
  }, { description: 'Returns current browser slot counts (headed/headless/appetize), limits, and runtime type (cpu/gpu).' });

  tool('xb_stream_list', {}, async () => {
    const { listStreams } = await import('./core/stream-registry.mjs');
    const { getResourceSnapshot } = await import('./core/resource-monitor.mjs');
    return { streams: listStreams(), resource: getResourceSnapshot() };
  }, { description: 'List all active browser streams (xio-main + appetize sessions) with FPS, client counts, and current resource levels.' });

  tool('xb_stream_register', {
    id:    z.string().describe('Unique stream ID (e.g. appetize-abc12345)'),
    label: z.string().describe('Human label for the stream'),
    type:  z.enum(['headed', 'headless', 'appetize']).optional().default('appetize'),
  }, async ({ id, label, type }) => {
    const { registerStream } = await import('./core/stream-registry.mjs');
    registerStream(id, label, type, async () => null, 2);
    return { ok: true, stream_id: id };
  }, { description: 'Register an external stream (e.g. broker Appetize session) in the stream registry. Frames are pushed via /stream/:id/push-frame.' });

  tool('xb_stream_unregister', {
    id: z.string().describe('Stream ID to unregister'),
  }, async ({ id }) => {
    const { unregisterStream } = await import('./core/stream-registry.mjs');
    unregisterStream(id);
    return { ok: true };
  }, { description: 'Unregister a stream from the registry and disconnect all its clients.' });

  // ── Action Recording ────────────────────────────────────────────────────

  tool('xb_start_recording', {
    name: z.string().optional().default('recorded_workflow')
            .describe('Descriptive name for the recorded workflow (used in filename)'),
  }, async ({ name }) => {
    recorder.start(name);
    return {
      success: true,
      message: `Recording started: ${name}. All xb_interact/xb_devtools actions will be captured.`,
    };
  }, { description: 'Start recording browser actions (navigate, click, type, evaluate) into a reusable workflow .mjs file. Sensitive values are auto-parameterized. Stop with xb_stop_recording.' });

  tool('xb_stop_recording', {
    job_id: z.string().optional().default('')
              .describe('Optional job ID for filename uniqueness'),
  }, async ({ job_id }) => {
    const outPath = recorder.emit(job_id);
    return {
      success:     !!outPath,
      output_path: outPath,
      message:     outPath ? `Workflow saved: ${outPath}` : 'No actions recorded.',
    };
  }, { description: 'Stop the active recording session and emit a reusable .mjs workflow file. The file is saved to workflows/recorded/ and immediately runnable via xb_run_workflow.' });

  tool('xb_interact', {
    session_id: z.string().describe('Session ID with an active browser context'),
    action:     z.enum(['click', 'type', 'navigate', 'screenshot', 'evaluate'])
                  .describe('Browser action type'),
    selector:   z.string().optional().describe('CSS selector (for click/type)'),
    value:      z.string().optional().describe('Text to type or URL to navigate to'),
    script:     z.string().optional().describe('JS expression to evaluate'),
    exit_node:  z.string().optional().describe('Tailscale exit node IP'),
  }, async ({ session_id, action, selector, value, script, exit_node }) => {
    const exitNode = exit_node ?? defaultExit;
    const { context } = await getContext(session_id, exitNode);
    const pages = context.pages();
    const page  = pages.length > 0 ? pages[pages.length - 1] : await context.newPage();
    const owned = pages.length === 0; // we created this page, so we close it

    try {
      switch (action) {
        case 'navigate':
          await page.goto(value, { waitUntil: 'domcontentloaded' });
          break;
        case 'click':
          await page.click(selector);
          break;
        case 'type':
          await page.fill(selector, value);
          break;
        case 'evaluate':
          // Record evaluate action before the early return so it's captured when recording is active
          if (recorder.active) recorder.record('evaluate', { script });
          return { result: await page.evaluate(script) };
        case 'screenshot':
          break;
      }

      // Record action if recording is active (evaluate is recorded above before early return)
      if (recorder.active && action && action !== 'evaluate') {
        recorder.record(action, {
          selector,
          value,
          key:    undefined,
          script,
        });
      }

      const buf = await page.screenshot({ type: 'jpeg', quality: 70, fullPage: false });
      return {
        ok:  true,
        url: page.url(),
        screenshot: `data:image/jpeg;base64,${buf.toString('base64')}`,
      };
    } finally {
      if (owned) await page.close().catch(() => {});
    }
  }, { description: 'Perform a single browser action (navigate, click, type, screenshot, evaluate) in an active session context. Returns a JPEG screenshot after each action. Use xb_run_workflow for multi-step automation.' });

  // ═══════════════════════════════════════════════════════════════════════
  // SESSION MANAGEMENT TOOLS
  //
  // A "session" is an identity slot = one persistent Chrome context.
  // One slot can be logged into Google AND V0 AND GitHub simultaneously.
  // Each logged-in service is tracked separately via session_services.
  // ═══════════════════════════════════════════════════════════════════════

  // List all identity slots with their active services
  tool('xb_session_list', {}, async () => {
    return listSessions(); // each row includes a .services[] array
  }, { description: 'List all session identity slots with their bound exit node, tier, persistence status, and logged-in services (google, v0, etc.).' });

  // Create a new identity slot (no service association — that comes from running workflows)
  tool('xb_session_create', {
    id:           z.string().describe('Unique slot ID, e.g. "acc50" or "main"'),
    display_name: z.string().optional().describe('Human-readable label (defaults to id)'),
    notes:        z.string().optional().describe('Optional notes, e.g. primary email address'),
    exit_node:    z.string().optional().describe(
      'Tailscale IP to pre-bind this session to. If omitted, binding happens automatically on first workflow run. ' +
      'Pre-binding is recommended when you know which exit node will own this session.'
    ),
  }, async ({ id, display_name, notes, exit_node }) => {
    upsertSession({ id, display_name, notes, exit_node });
    return {
      ok: true, id,
      exit_node: exit_node ?? null,
      message: exit_node
        ? `Session "${id}" created and pre-bound to exit node ${exit_node}.`
        : `Session "${id}" created. Exit node will be auto-bound on first workflow run.`,
    };
  }, { description: 'Create a new session identity slot. Each slot holds one persistent Chrome context that can be logged into multiple services simultaneously. Use the full email as the id (e.g. user@gmail.com).' });

  // Delete a slot and all its Drive files
  tool('xb_session_delete', {
    id: z.string().describe('Slot ID to delete'),
  }, async ({ id }) => {
    deleteSession(id);
    deleteStorageState(id);
    return { ok: true, deleted: id };
  }, { description: 'Delete a session slot: removes the DB row, cookie JSON, and triggers Drive deletion of the profile tarball. This is irreversible.' });

  // Manually record that a specific service is logged in within a slot
  // (workflows call this automatically on success)
  // Accepts BOTH 'id' and 'session_id' as aliases (skill guide uses session_id).
  tool('xb_session_service_upsert', {
    id:           z.string().optional().describe('Slot ID (slug or full email). Alias: session_id.'),
    session_id:   z.string().optional().describe('Slot ID alias — accepts slug, underscore-slug, or full email.'),
    service:      z.string().describe('Service name, e.g. "v0", "google", "github"'),
    account_hint: z.string().optional().describe('Email or username for reference'),
    valid:        z.boolean().optional().default(true).describe('Whether this service session is currently valid'),
  }, async ({ id, session_id, service, account_hint, valid }) => {
    const rawId = id ?? session_id;
    if (!rawId) return { error: 'id or session_id is required' };
    const normId = normaliseSessionId(rawId);  // slug, underscore-slug, or email → canonical slug
    const session = getSession(normId);
    if (!session) return { error: `Slot not found: ${normId} (resolved from: ${rawId})` };
    upsertSessionService({ session_id: normId, service, account_hint, session_valid: valid });
    return { ok: true, id: normId, service, valid };
  }, { description: 'Manually record or update which service a session is logged into (e.g. google, v0, github). Accepts id or session_id (slug, underscore-slug, or full email). Workflows call this automatically on success.' });

  // Live-check whether a specific service is still logged in within a slot
  // ⚠️  READ-ONLY GUARANTEE: xb_session_status intentionally does NOT write back
  // to the session JSON file. Context creation now uses loadAndHydrateContext()
  // (v2 soft-persistence) which is also read-only with respect to the file.
  // Rotating cookie renewal happens automatically via warm-up navigations in
  // loadAndHydrateContext() — not here.
  tool('xb_session_status', {
    id:                 z.string().describe('Slot ID'),
    service:            z.string().describe('Service to check, e.g. "v0", "google"'),
    verify_url:         z.string().describe('URL to navigate to for verification'),
    logged_in_selector: z.string().optional().describe('CSS selector present when logged in'),
    exit_node:          z.string().optional().describe('Tailscale exit node IP'),
  }, async ({ id, service, verify_url, logged_in_selector, exit_node }) => {
    const session = getSession(id);
    if (!session) return { error: `Slot not found: ${id}` };

    const exitNode = exit_node ?? defaultExit;
    const { context } = await getContext(id, exitNode);
    const page = await context.newPage();

    try {
      await page.goto(verify_url, { waitUntil: 'domcontentloaded', timeout: 15000 });
      const valid = logged_in_selector
        ? await page.$(logged_in_selector).then(el => !!el)
        : !page.url().includes('login') && !page.url().includes('signin');

      markServiceValid(id, service, valid);
      const buf = await page.screenshot({ type: 'jpeg', quality: 70, fullPage: false });
      return {
        id, service,
        session_valid: valid,
        url:           page.url(),
        screenshot:    `data:image/jpeg;base64,${buf.toString('base64')}`,
      };
    } finally {
      await page.close().catch(() => {});
    }
  }, { description: 'Live-verify whether a session is still logged into a specific service by navigating to a URL and checking a CSS selector or URL pattern. Updates the session_services record and returns a screenshot.' });

  // Export the full storageState blob (contains ALL domain cookies in this slot)
  tool('xb_session_export', {
    id: z.string().describe('Slot ID to export'),
  }, async ({ id }) => {
    const state = loadStorageState(id);
    if (!state) return { error: `No saved state for slot: ${id}` };
    const cookieDomains = [...new Set((state.cookies ?? []).map(c => c.domain))];
    return { id, cookie_domains: cookieDomains, storageState: state };
  }, { description: 'Export the full Playwright storageState blob (cookies + localStorage) for a session slot. Use xb_session_import to restore it on another node.' });

  // Import an external storageState blob into a slot
  tool('xb_session_import', {
    id:           z.string().describe('Slot ID to import into (auto-created if not exists)'),
    storageState: z.object({
      cookies: z.array(z.any()).optional(),
      origins: z.array(z.any()).optional(),
    }).describe('Playwright storageState with cookies + origins for one or more domains'),
  }, async ({ id, storageState }) => {
    // Auto-create session slot if it doesn't exist yet
    upsertSession({ id, display_name: id });
    saveStorageState(id, storageState);
    const cookieDomains = [...new Set((storageState.cookies ?? []).map(c => c.domain))];
    return { ok: true, id, cookie_domains: cookieDomains, cookies_count: (storageState.cookies ?? []).length };
  }, { description: 'Import a Playwright storageState blob (cookies + localStorage) into a session slot. The slot is auto-created if it does not exist. Use with xb_session_export to move sessions between nodes.' });

  // ── Session health check: cookie expiry analysis across all sessions ──────
  //    Returns a structured health report with expiring/expired rotating cookies
  //    and an urgency level: none | scheduled | soon | immediate
  //    When urgency is 'soon' or 'immediate', run google-session-refresh workflow.
  tool('xb_session_health', {
    session_id: z.string().optional().describe(
      'Specific session to check (omit to check ALL sessions in /sessions dir)'
    ),
  }, async ({ session_id } = {}) => {
    if (session_id) {
      // Use canonical path (PRFL-NNN_username.json) with legacy fallback
      const sessionPath = sessionStatePath(session_id);
      const health = checkCookieHealth(sessionPath);
      return { session_id, health };
    }
    // All sessions — scan sessions dir for both PRFL-* and legacy *.json files
    const { Paths: DrvPaths } = await import('./utils/drive.mjs');
    const sessionsDir = DrvPaths.sessions();
    const all = checkAllSessionsHealth(sessionsDir);
    const summary = {
      total:        all.length,
      ok:           all.filter(s => s.health.ok).length,
      need_refresh: all.filter(s => s.health.needs_refresh).length,
      immediate:    all.filter(s => s.health.refresh_urgency === 'immediate').map(s => s.session_id),
      soon:         all.filter(s => s.health.refresh_urgency === 'soon').map(s => s.session_id),
      scheduled:    all.filter(s => s.health.refresh_urgency === 'scheduled').map(s => s.session_id),
    };
    return { summary, sessions: all };
  }, { description: 'Analyse Google session cookie health: checks expiry of rotating auth tokens (SIDCC, PSIDTS, etc.) and returns urgency level (none|scheduled|soon|immediate). Run google-session-refresh when urgency is soon or immediate.' });

  // ═══════════════════════════════════════════════════════════════════════
  // WORKFLOW PLUGIN MANAGEMENT TOOLS
  // ═══════════════════════════════════════════════════════════════════════

  tool('xb_workflow_list', {}, async () => {
    return listWorkflows().map(w => {
      const meta = getWorkflowMeta(w.id) ?? {};
      return { id: w.id, ...meta };
    });
  }, { description: 'List all available workflow plugins with their metadata (description, params schema, version). Workflows are loaded from the bundled git repo and any Drive overrides.' });

  tool('xb_workflow_get', {
    id: z.string().describe('Workflow ID'),
  }, async ({ id }) => {
    const src = getWorkflowSource(id);
    if (!src) return { error: `Workflow not found: ${id}` };
    return { id, source: src };
  }, { description: 'Get the full source code of a workflow plugin by ID. Useful for understanding what a workflow does before running it or for debugging.' });

  tool('xb_workflow_push', {
    id:     z.string().describe('Workflow ID (used as filename: {id}.mjs)'),
    source: z.string().describe('Full .mjs source code of the workflow'),
  }, async ({ id, source }) => {
    // Validate basic structure
    if (!source.includes('export async function run(') && !source.includes('export function run(')) {
      return { ok: false, error: 'Source must export a run() function. Add: export async function run(ctx, params) { ... }' };
    }
    const result = patchWorkflowFile(id, source);
    return {
      ...result,
      id,
      message: `Workflow "${id}" hot-patched. Active immediately — use xb_commit_patch to persist to git.`,
      commit_hint: 'Call xb_commit_patch to commit this change to the git repository.',
    };
  }, { description: 'Hot-patch a workflow .mjs file directly on this Colab node. The workflow is immediately available for xb_run_workflow without restarting. Use xb_commit_patch afterwards to persist the change to git.' });

  tool('xb_workflow_delete', {
    id: z.string().describe('Workflow ID to delete'),
  }, async ({ id }) => {
    // deleteWorkflow returns { ok: false, error } — workflows are git-managed
    return { ...deleteWorkflow(id), id };
  }, { description: 'Delete a workflow plugin by ID. NOTE: workflows are git-managed — this always returns ok:false with guidance to remove from the repository instead.' });

  // ═══════════════════════════════════════════════════════════════════════
  // HOT-PATCH TOOLS (xb_patch, xb_patch_rollback, xb_commit_patch)
  // ═══════════════════════════════════════════════════════════════════════

  // Import patch ledger lazily (same Node process, no module isolation issues)
  let _ledger;
  async function getLedger() {
    if (!_ledger) {
      const m = await import('./core/patch-ledger.mjs');
      _ledger = m;
    }
    return _ledger;
  }

  tool('xb_patch', {
    rel_path:    z.string().describe(
      'File path relative to /content/xio-browser (e.g. "workflows/self-spawn.mjs" or "src/core/job-manager.mjs")'
    ),
    content:     z.string().describe('New file content to write'),
    description: z.string().optional().default('').describe('Human description of the change (for commit message)'),
  }, async ({ rel_path, content, description }) => {
    const ledger = await getLedger();
    // Write the file
    const result = patchArbitraryFile(rel_path, content);
    // Record in ledger for later git commit
    ledger.recordPatch(result.abs_path, content, description);
    log.info(`[xb_patch] Patched ${rel_path}`);
    return {
      ok:          true,
      rel_path,
      abs_path:    result.abs_path,
      patched_at:  new Date().toISOString(),
      description,
      message:     `File patched. Changes are live immediately. Call xb_commit_patch to persist to git.`,
      ledger_size: ledger.listPatches().length,
    };
  }, { description: [
    'Hot-patch any file in the xio-browser repository directly on the Colab node.',
    'Changes take effect immediately — no restart needed.',
    'For workflow .mjs files: the next xb_run_workflow call picks up the new version.',
    'For server source files (src/): restart with xb_restart to apply.',
    'Use xb_commit_patch to persist all pending patches to the git repository.',
  ].join(' ') });

  tool('xb_patch_rollback', {
    rel_path: z.string().optional().describe('Specific file to roll back. Omit to roll back ALL patches.'),
  }, async ({ rel_path }) => {
    const ledger = await getLedger();
    if (rel_path) {
      const absPath = path.resolve('/content/xio-browser', rel_path);
      return ledger.rollbackPatch(absPath);
    }
    // Roll back all
    const patches = ledger.listPatches();
    const results = patches.map(p => ledger.rollbackPatch(p.file));
    return { ok: true, rolled_back: results.length, files: patches.map(p => p.basename) };
  }, { description: 'Roll back hot-patches applied via xb_patch. Restores original file content. Pass rel_path to roll back a specific file, or omit to roll back everything.' });

  tool('xb_commit_patch', {
    message: z.string().optional().default('').describe('Git commit message (auto-generated if empty)'),
    push:    z.boolean().optional().default(true).describe('Push to remote after commit (default: true)'),
  }, async ({ message, push }) => {
    const ledger = await getLedger();
    const patches = ledger.listPatches();
    if (patches.length === 0) {
      return { ok: false, error: 'No pending patches to commit. Apply patches first with xb_patch.' };
    }

    const files   = patches.map(p => p.file);
    const descriptions = patches.map(p => p.description).filter(Boolean);
    const msg = message || `feat: hot-patch [⁠${patches.map(p => p.basename).join(', ')}]

${descriptions.join('\n')}`;

    try {
      // Stage all patched files
      execSync(`git -C /content/xio-browser add ${files.map(f => JSON.stringify(f)).join(' ')}`, {
        stdio: 'pipe', encoding: 'utf8'
      });
      // Commit
      execSync(`git -C /content/xio-browser commit -m ${JSON.stringify(msg)}`, {
        stdio: 'pipe', encoding: 'utf8'
      });
      // Optionally push
      let pushed = false;
      if (push) {
        try {
          execSync('git -C /content/xio-browser push origin main', {
            stdio: 'pipe', encoding: 'utf8', timeout: 30_000
          });
          pushed = true;
        } catch (pe) {
          log.warn(`[xb_commit_patch] push failed (non-fatal): ${pe.message}`);
        }
      }
      ledger.clearLedger();
      return {
        ok:          true,
        committed:   files.map(f => f.split('/').pop()),
        message:     msg,
        pushed,
        hint:        pushed
          ? 'Patches committed and pushed to git. Workers will pull automatically on next keep-alive cycle.'
          : 'Patches committed locally. Push manually or call xb_commit_patch again to push.',
      };
    } catch (e) {
      return { ok: false, error: e.message };
    }
  }, { description: 'Commit all pending hot-patches (applied via xb_patch) to the git repository and optionally push to remote. This makes the changes permanent and propagates them to all workers on next git pull.' });

  // ═══════════════════════════════════════════════════════════════════════
  // WORKFLOW PAUSE / RESUME TOOLS
  // ═══════════════════════════════════════════════════════════════════════

  tool('xb_job_pause', {
    job_id: z.string().describe('Job ID of the running workflow to pause'),
    reason: z.string().optional().default('Agent-requested pause').describe('Reason for pausing (logged in checkpoint)'),
  }, async ({ job_id, reason }) => {
    // Signal the running job to pause at the next ctx.isCancelled() checkpoint
    // The job must call ctx.pause() itself (from _test-runner HITL or explicit workflow code).
    // We record the intent here so the next HITL escalation auto-pauses instead of erroring.
    globalThis.__xioPauseRequested = globalThis.__xioPauseRequested ?? new Map();
    globalThis.__xioPauseRequested.set(job_id, { reason, ts: new Date().toISOString() });
    log.info(`[xb_job_pause] Pause requested for job ${job_id}: ${reason}`);
    return {
      ok:      true,
      job_id,
      reason,
      message: 'Pause requested. The job will pause at its next HITL or checkpoint step. Poll xb_job_poll to confirm status = "paused".',
    };
  }, { description: 'Request a running workflow to pause at its next safe checkpoint (HITL step). The job suspends its browser context and waits for xb_job_resume.' });

  tool('xb_job_resume', {
    job_id: z.string().describe('Job ID of the paused workflow to resume'),
  }, async ({ job_id }) => {
    const result = resumePausedJob(job_id);
    return result;
  }, { description: 'Resume a paused workflow job. The job continues from the step where it paused. Use after applying hot-patches with xb_patch to fix the underlying issue.' });

  tool('xb_job_paused_list', {}, async () => {
    return listPausedJobs();
  }, { description: 'List all currently paused workflow jobs with their step index, pause reason, and checkpoint info.' });


  tool('xb_node_status', {}, async () => {
    const status = getPeerStatus();
    const ip     = selfIP();
    const port   = config.httpPort ?? 4242;
    const currentDefaultExit = getDefaultExitNode() ?? defaultExit;

    // Separate concept: exit node (web traffic routing) vs client node (AI agent connection)
    // The client node is whatever Tailscale peer called this API — it is NOT necessarily the exit node.
    // Exit node is the device whose IP + fingerprint Colab impersonates for all browser requests.
    const peers = listAvailablePeers();
    const exitNodePeer   = peers.find(p => p.tailscale_ips.includes(currentDefaultExit));
    const exitNodePeers  = peers.filter(p => p.is_exit_node); // peers advertising as exit nodes

    return {
      server:        'xio-browser v0.4.0',
      runtime_node: {
        description: 'Colab runtime running the MCP server and browser',
        colab_ip:    ip,
        mcp_url:     ip ? `http://${ip}:${port}/mcp` : null,
      },
      exit_node: {
        description: 'Device whose network identity (IP + fingerprint) is used for all browser requests. ' +
                     'DIFFERENT from the client node (agent) and DIFFERENT from the Colab runtime node.',
        current_ip:  currentDefaultExit,
        hostname:    exitNodePeer?.hostname ?? null,
        os:          exitNodePeer?.os ?? null,
        online:      exitNodePeer?.online ?? null,
        change_with: 'xb_exit_node_set',
      },
      client_node: {
        description: 'The AI agent or user connecting to this MCP server. NOT the exit node. ' +
                     'The client can be on a Mac, cloud, or anywhere on the Tailnet.',
        note: 'Client identity is inferred from the incoming request — not tracked explicitly.',
      },
      available_exit_nodes: exitNodePeers.map(p => ({
        hostname: p.hostname, ip: p.primary_ip, os: p.os, online: p.online,
        is_current_default: p.is_current_default,
      })),
      all_peers:     peers,
      drive_root:    driveRoot,
      // Live monitoring URLs
      stream_url:    ip ? `http://${ip}:${port}/stream`   : null,
      devtools_url:  ip ? `http://${ip}:${port}/devtools` : null,
      events_url:    ip ? `http://${ip}:${port}/events`   : null,
    };
  }, { description: 'Get status of this Colab node: Tailscale IP, current exit node (whose IP the browser impersonates), available exit node peers, and live monitoring URLs (stream, devtools, events).' });

  // ── Runtime Management ─────────────────────────────────────────────────────
  tool(runtimeConfigTool.name, {
    action: z.enum(['get', 'set', 'list']).describe('Action: get current config, set a key, or list all node configs on Drive.'),
    key:    z.string().optional().describe('Config key to get or set (e.g. auto_spawn_enabled, auto_spawn_after_minutes, default_exit).'),
    value:  z.union([z.boolean(), z.number(), z.string()]).optional().describe('Value to set (boolean, number, or string).'),
    scope:  z.enum(['node', 'global']).optional().default('node').describe('node = this runtime only (default); global = all runtimes.'),
    node:   z.string().optional().describe('Target node name (default: this node). For set, writes to that node\'s config.'),
  }, runtimeConfigTool.handler, { description: runtimeConfigTool.description });

  tool(runtimeDisconnectTool.name, {
    node:   z.string().optional().default('self').describe('Node to disconnect: "self" (default) or a node name like "colab-worker-karmareturnsfromallsides".'),
    reason: z.string().optional().default('manual disconnect').describe('Optional reason for disconnection (logged).'),
  }, runtimeDisconnectTool.handler, { description: runtimeDisconnectTool.description });

  tool(runtimeSpawnTool.name, {
    session_id: z.string().optional().describe('Session to spawn with. Defaults to best available persisted Pro session.'),
    node:       z.string().optional().default('self').describe('Node to trigger spawn on: "self" or a remote node name.'),
    params:     z.record(z.any()).optional().describe('Extra params forwarded to the self-spawn workflow.'),
  }, runtimeSpawnTool.handler, { description: runtimeSpawnTool.description });

  tool(saveContextsTool.name, {}, saveContextsTool.handler, { description: saveContextsTool.description });

  // ── Dynamic Exit Node Control ──────────────────────────────────────────────
  // Change which Tailscale peer is the default exit node for new workflows.
  // Does NOT affect currently-running jobs or existing session bindings.
  tool('xb_exit_node_set', {
    exit_node_ip: z.string().describe(
      'Tailscale IP of the peer to use as the new default exit node. ' +
      'Get available IPs from xb_node_status → all_peers. ' +
      'WARNING: changing the default does not change existing session bindings — ' +
      'only affects new sessions/jobs that have no prior binding.'
    ),
  }, async ({ exit_node_ip }) => {
    // Validate the IP is a known peer
    const peers = listAvailablePeers();
    const target = peers.find(p => p.tailscale_ips.includes(exit_node_ip));
    if (!target) {
      return {
        ok: false,
        error: `Peer ${exit_node_ip} not found in Tailnet. Available peers: ${peers.map(p => p.primary_ip + '(' + p.hostname + ')').join(', ')}`,
      };
    }
    // Apply at network level
    await setExitNode(exit_node_ip);
    // Update runtime default (used for all new jobs where exit_node is not specified)
    setDefaultExitNode(exit_node_ip);
    return {
      ok:           true,
      exit_node_ip,
      hostname:     target.hostname,
      os:           target.os,
      message:      `Default exit node updated to ${target.hostname} (${exit_node_ip}). ` +
                    `Existing session bindings are NOT affected. ` +
                    `Only new unbound sessions will use this exit node by default.`,
      warning: target.online === false
        ? `⚠️ Peer ${target.hostname} appears OFFLINE. Traffic may not route correctly.`
        : null,
    };
  }, { description: 'Change the default Tailscale exit node (the device whose residential IP and hardware fingerprint the browser impersonates). Only affects new unbound sessions — existing session bindings are NOT changed.' });

  // ── Remote shell execution (for agent autonomy) ──────────────────────────
  // Lets the agent run any shell command on the Colab runtime via MCP.
  tool('xb_shell', {
    cmd:     z.string().describe('Shell command to run (runs in /bin/bash -c)'),
    timeout: z.number().optional().default(30000).describe('Timeout in ms (default 30s)'),
  }, async ({ cmd, timeout }) => {
    log.info(`[xb_shell] ${cmd}`);
    try {
      const out = execSync(cmd, {
        shell: '/bin/bash',
        stdio: 'pipe',
        timeout,
        encoding: 'utf8',
        maxBuffer: 1024 * 1024, // 1 MB
      });
      return { ok: true, stdout: out, stderr: '' };
    } catch (e) {
      return {
        ok:     false,
        stdout: e.stdout ?? '',
        stderr: e.stderr ?? e.message,
        code:   e.status ?? -1,
      };
    }
  }, { description: 'Execute any shell command on the Colab runtime (runs in /bin/bash). Returns stdout, stderr, and exit code. Use for diagnostics, file operations, or running Python scripts directly.' });


  // ── Log tailing (for debugging without SSH) ──────────────────────────────
  tool('xb_log_tail', {
    log:   z.enum(['xiobr', 'xiov0', 'tailscaled', 'syslog']).optional().default('xiobr')
              .describe('Which log to tail'),
    lines: z.number().optional().default(50).describe('Number of lines to return'),
  }, async ({ log: logFile, lines }) => {
    const logPaths = {
      xiobr:      '/tmp/xiobr.log',
      xiov0:      '/tmp/xiov0.log',
      tailscaled: '/tmp/tailscaled.log',
      syslog:     '/var/log/syslog',
    };
    try {
      const out = execSync(`tail -n ${lines} "${logPaths[logFile]}"`, {
        shell: '/bin/bash', stdio: 'pipe', encoding: 'utf8',
      });
      return { log: logFile, lines: out.split('\n').filter(Boolean) };
    } catch (e) {
      return { error: e.message };
    }
  }, { description: 'Tail the last N lines of a service log (xiobr, xiov0, tailscaled, or syslog). Use for real-time debugging without SSH access.' });

  // ── D1 query (direct CF D1 SQL access) ───────────────────────────────────
  tool('xb_d1_query', {
    sql:    z.string().describe('SQL query to run against the CF D1 database'),
    params: z.array(z.any()).optional().default([]).describe('Optional positional params for the query'),
  }, async ({ sql, params }) => {
    try {
      const { getD1Client } = await import('./core/d1.mjs');
      const client = getD1Client();
      const rows = await client.query(sql, params);
      return { ok: true, rows, count: rows.length };
    } catch (e) {
      return { ok: false, error: e.message, sql };
    }
  }, { description: 'Run any SQL SELECT (or write) against the Cloudflare D1 database. Returns rows as an array of objects. Useful for inspecting accounts, sessions, node_registry, node_secrets, runtime_config, etc. Tables: accounts, sessions, session_credentials, node_registry, node_secrets, node_pubkeys, locks, runtime_config, _cf_KV.' });

  // ── R2 list (list objects in the xio-mesh R2 bucket) ─────────────────────
  tool('xb_r2_list', {
    prefix:  z.string().optional().default('').describe('Key prefix to filter by (e.g. "ts_states/", "cache/", "")'),
    maxKeys: z.number().optional().default(200).describe('Max number of keys to return'),
  }, async ({ prefix, maxKeys }) => {
    try {
      // Load R2 creds from D1 node_secrets
      const { getD1Client } = await import('./core/d1.mjs');
      const client = getD1Client();
      const secrets = await client.query(
        "SELECT key, value FROM node_secrets WHERE key IN ('r2_endpoint','r2_access_key_id','r2_secret_access_key','r2_bucket')"
      );
      const creds = Object.fromEntries(secrets.map(r => [r.key, r.value]));
      if (!creds.r2_endpoint) throw new Error('R2 creds not found in node_secrets');

      // Use boto3 via shell (avoids ESM AWS SDK import complexity)
      const script = [
        'import boto3,json',
        `s3=boto3.client("s3",endpoint_url=${JSON.stringify(creds.r2_endpoint)},`,
        `aws_access_key_id=${JSON.stringify(creds.r2_access_key_id)},`,
        `aws_secret_access_key=${JSON.stringify(creds.r2_secret_access_key)},`,
        `region_name="auto")`,
        `res=s3.list_objects_v2(Bucket=${JSON.stringify(creds.r2_bucket)},Prefix=${JSON.stringify(prefix)},MaxKeys=${maxKeys})`,
        `objs=res.get("Contents",[])`,
        `pfxs=[p["Prefix"] for p in res.get("CommonPrefixes",[])]`,
        `print(json.dumps({"objects":objs,"prefixes":pfxs,"count":len(objs)}),default=str)`,
      ].join(';');
      const out = execSync(`python3 -c '${script.replace(/'/g, "'\\''")}'`, {
        shell: '/bin/bash', stdio: 'pipe', encoding: 'utf8', timeout: 15_000,
      });
      return JSON.parse(out);
    } catch (e) {
      return { ok: false, error: e.message };
    }
  }, { description: 'List objects in the xio-mesh R2 bucket. Use prefix="ts_states/" to see Tailscale state files, prefix="cache/" for node caches, prefix="" for all root prefixes. Returns {objects, prefixes, count}.' });

  // ═══════════════════════════════════════════════════════════════════════
  // CHROME DEVTOOLS PROTOCOL (CDP) TOOLS
  // Full remote browser control via Playwright's CDP session.
  // These tools attach to the LIVE running job page OR any open session.
  // ═══════════════════════════════════════════════════════════════════════

  // ── Helper: resolve the live page for CDP tools ────────────────────────────
  async function resolveDevPage(session_id) {
    // Priority 1: live page from a currently-running job
    const running = getRunningJob();
    if (running && (!session_id || running.sessionId === session_id)) {
      return { context: running.context, page: running.page, live: true, jobId: running.jobId };
    }
    // Priority 2: open page in an existing browser context (or create one)
    if (session_id) {
      try {
        const { context } = await getContext(session_id, defaultExit);
        const pages = context.pages();
        // Reuse an existing open page, or open a blank one in the authenticated context
        const page = pages.length > 0
          ? pages[pages.length - 1]
          : await context.newPage();
        return { context, page, live: false };
      } catch { /* context may not exist */ }
    }
    throw new Error(
      'No active browser page found. Either pass session_id to open a page in that ' +
      'session\'s authenticated context, or start a workflow with xb_run_workflow first.'
    );
  }



  // xb_devtools_screenshot — CDP screenshot with quality/format control
  tool('xb_devtools_screenshot', {
    session_id: z.string().optional().describe('Session ID (omit to use the live running job)'),
    format:     z.enum(['png', 'jpeg', 'webp']).optional().default('jpeg').describe('Image format'),
    quality:    z.number().min(1).max(100).optional().default(70).describe('JPEG/WebP quality (default 70 — readable text, ~3x smaller than PNG). Use 90+ for pixel-perfect analysis.'),
    full_page:  z.boolean().optional().default(false).describe('Capture full scrollable page'),
  }, async ({ session_id, format, quality, full_page }) => {
    const { context, page, live, jobId } = await resolveDevPage(session_id);
    const b64 = await cdpScreenshot(context, page, { format, quality, fullPage: full_page });
    return {
      url:        page.url(),
      format,
      live_job:   live ? jobId : null,
      screenshot: `data:image/${format};base64,${b64}`,
    };
  }, { description: 'Take a screenshot of the active browser page via CDP. Supports JPEG/PNG/WebP with quality control. Attaches to the live running job or any open session context.' });

  // xb_devtools_evaluate — Runtime.evaluate with full V8 access
  tool('xb_devtools_evaluate', {
    session_id:    z.string().optional().describe('Session ID (omit for live running job)'),
    expression:    z.string().describe('JavaScript expression to evaluate in the page context'),
    await_promise: z.boolean().optional().default(true).describe('Await if expression returns a Promise'),
  }, async ({ session_id, expression, await_promise }) => {
    const { context, page } = await resolveDevPage(session_id);
    const value = await cdpEvaluate(context, page, expression, { awaitPromise: await_promise });
    return { url: page.url(), result: value };
  }, { description: 'Evaluate a JavaScript expression in the page via CDP Runtime.evaluate. Supports async/await and returns the serialized result. Full V8 access including console API.' });

  // xb_devtools_click — click at precise x,y coordinates (bypasses Playwright locators)
  tool('xb_devtools_click', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    x:          z.number().describe('X coordinate in CSS pixels'),
    y:          z.number().describe('Y coordinate in CSS pixels'),
    button:     z.enum(['left', 'right', 'middle']).optional().default('left'),
    count:      z.number().optional().default(1).describe('Click count (2 for double-click)'),
  }, async ({ session_id, x, y, button, count }) => {
    const { context, page } = await resolveDevPage(session_id);
    await cdpClick(context, page, x, y, { button, clickCount: count });
    if (recorder.active) recorder.record('xb_devtools_click', { x, y, button, count });
    await new Promise(r => setTimeout(r, 300));
    const b64 = await cdpScreenshot(context, page, { format: 'jpeg', quality: 80 });
    return { ok: true, x, y, url: page.url(), screenshot: `data:image/jpeg;base64,${b64}` };
  }, { description: 'Click at precise x,y pixel coordinates via CDP Input.dispatchMouseEvent. Bypasses Playwright locators — useful for clicking elements without stable CSS selectors. Returns a post-click screenshot.' });

  // xb_devtools_type — type text into focused element via CDP key events
  tool('xb_devtools_type', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    text:       z.string().describe('Text to type (char-by-char key events)'),
    delay_ms:   z.number().optional().default(25).describe('Delay between key events in ms'),
  }, async ({ session_id, text, delay_ms }) => {
    const { context, page } = await resolveDevPage(session_id);
    await cdpType(context, page, text, { delayMs: delay_ms });
    if (recorder.active) recorder.record('xb_devtools_type', { text, delay_ms });
    return { ok: true, typed: text.length, url: page.url() };
  }, { description: 'Type text character-by-character via CDP key events into the currently focused element. Works even without element focus via preceding xb_devtools_click. More realistic than Playwright fill().' });

  // xb_devtools_dom — get full page HTML via CDP
  tool('xb_devtools_dom', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    max_bytes:  z.number().optional().default(200000).describe('Max HTML bytes to return'),
  }, async ({ session_id, max_bytes }) => {
    const { context, page } = await resolveDevPage(session_id);
    const html = await cdpGetDOM(context, page);
    const truncated = html.length > max_bytes;
    return {
      url:       page.url(),
      html:      truncated ? html.slice(0, max_bytes) + '\n<!-- TRUNCATED -->' : html,
      truncated,
      bytes:     html.length,
    };
  }, { description: 'Get the full outer HTML of the current page DOM via CDP. Useful for analyzing page structure, finding selectors, or scraping content. Truncated at max_bytes (default 200KB).' });

  // xb_devtools_cookies — get all cookies for the current page URL
  tool('xb_devtools_cookies', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
  }, async ({ session_id }) => {
    const { context, page } = await resolveDevPage(session_id);
    const cookies = await cdpGetCookies(context, page);
    return { url: page.url(), count: cookies.length, cookies };
  }, { description: 'Get all cookies visible to the current page URL via CDP Network.getCookies. To get ALL browser cookies across all domains (e.g. Google session cookies), use xb_devtools_command with Network.getAllCookies.' });

  // xb_devtools_network_log — buffered network requests from the current page
  tool('xb_devtools_network_log', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    last:       z.number().optional().default(50).describe('Return last N requests'),
    filter_url: z.string().optional().describe('Filter requests by URL substring'),
  }, async ({ session_id, last, filter_url }) => {
    const { context, page } = await resolveDevPage(session_id);
    // Ensure monitoring is active
    await getCDPSession(context, page);
    let requests = getNetworkLog(page, { last: 300 });
    if (filter_url) requests = requests.filter(r => r.url.includes(filter_url));
    return { url: page.url(), count: requests.length, requests: requests.slice(-last) };
  }, { description: 'Return the buffered network request log for the current page (last 300 requests). Shows URL, method, status, and MIME type. Filter by URL substring. Monitoring starts automatically when CDP session opens.' });

  // xb_devtools_console_log — buffered console messages from the current page
  tool('xb_devtools_console_log', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    last:       z.number().optional().default(100).describe('Return last N messages'),
    level:      z.enum(['error', 'warning', 'info', 'log', 'all']).optional().default('all'),
  }, async ({ session_id, last, level }) => {
    const { context, page } = await resolveDevPage(session_id);
    await getCDPSession(context, page);
    let messages = getConsoleLog(page, { last: 500 });
    if (level !== 'all') messages = messages.filter(m => m.level === level);
    return { url: page.url(), count: messages.length, messages: messages.slice(-last) };
  }, { description: 'Return buffered browser console messages (last 500 entries) from the current page. Filter by level (error, warning, info, log). Useful for debugging JS errors and page behaviour.' });

  // xb_devtools_command — send any raw CDP command (full DevTools Protocol access)
  tool('xb_devtools_command', {
    session_id: z.string().optional().describe('Session ID (omit for live running job)'),
    method:     z.string().describe('CDP method, e.g. "Page.reload", "Runtime.evaluate"'),
    params:     z.record(z.unknown()).optional().default({}).describe('CDP method parameters'),
  }, async ({ session_id, method, params }) => {
    const { context, page } = await resolveDevPage(session_id);
    const result = await cdpSend(context, page, method, params);
    return { method, result };
  }, { description: 'Send any raw Chrome DevTools Protocol command to the page. Full access to all CDP domains (Page, Runtime, Network, DOM, Emulation, etc.). Example: method="Page.reload", method="Network.getAllCookies".' });

  // xb_devtools_url — returns endpoints for connecting Mac's chrome-devtools MCP
  tool('xb_devtools_url', {
    session_id: z.string().optional().describe('Session ID to get devtools URL for'),
  }, async ({ session_id }) => {
    const wsEndpoint = await getBrowserWSEndpoint().catch(() => null);
    const tsIP       = selfIP();
    const { context, page } = await resolveDevPage(session_id).catch(() => ({ context: null, page: null }));

    // Port 9222 is bound to 0.0.0.0 (including Tailscale interface)
    const devtoolsHttp = `http://${tsIP}:9222`;
    const activePage   = page ? {
      url:   page.url(),
      title: await page.title().catch(() => ''),
    } : null;

    return {
      // Connect Mac's chrome-devtools MCP to this URL:
      devtools_http:   devtoolsHttp,
      devtools_json:   `${devtoolsHttp}/json`,
      playwright_ws:   wsEndpoint,
      colab_ip:        tsIP,
      active_page:     activePage,
      ssh_tunnel_cmd:  `ssh -L 9222:localhost:9222 root@${tsIP}  # Then open chrome://inspect on Mac`,
      mac_chrome_devtools_config: {
        note: 'Add this to your Mac\'s chrome-devtools MCP config after SSH tunneling:',
        url:  'http://localhost:9222',
      },
    };
  }, { description: 'Get the Chrome DevTools remote debugging URL and Playwright WebSocket endpoint for this Colab browser. Use the SSH tunnel command to connect Mac\'s chrome-devtools MCP directly to the live browser.' });

  // ── Drive sync — pull latest state from Drive into running instance ─────────
  // Syncs sessions and the SQLite DB from Drive → local without
  // requiring a server restart. Workflows are git-managed — use xb_restart (pull_code:true)
  // to get latest workflow changes. For a deeper sync (new DB state), use xb_restart instead.
  tool('xb_sync', {
    what: z.enum(['all', 'sessions', 'db'])
              .optional().default('all')
              .describe('What to sync from Drive (default: all)'),
  }, async ({ what }) => {
    const syncScript = '/content/xio-browser/colab/sync.py';
    const hasSyncScript = fs.existsSync(syncScript);

    if (!hasSyncScript) {
      return {
        ok:      false,
        message: 'sync.py not found — run start.ipynb Cell 2 first to generate it, or use xb_restart for a full reboot.',
        hint:    'xb_restart performs a full restart including Drive sync in boot.py Phase 6.',
      };
    }

    log.info(`[xb_sync] Running sync.py (what=${what})…`);
    try {
      const out = execSync(`python3 ${syncScript} --what ${what}`, {
        shell:   '/bin/bash',
        stdio:   'pipe',
        timeout: 120_000,   // 2 min — Drive API can be slow
        encoding: 'utf8',
      });
      // After sync, list what's now available
      const workflows = listWorkflows();
      return {
        ok:        true,
        synced:    what,
        output:    out.trim().split('\n'),
        workflows: workflows.map(w => ({ id: w.id, source: w.source ?? 'bundled' })),
      };
    } catch (e) {
      return {
        ok:     false,
        error:  e.stderr || e.message,
        hint:   'If credentials expired, restart the Colab runtime or re-run start.ipynb.',
      };
    }
  }, { description: 'Pull the latest session cookies, Chrome profile tarballs, and/or the SQLite DB from Google Drive into the running instance. Workflows are git-managed — use xb_restart with pull_code:true to get latest workflow changes.' });

  // ── Full restart — git pull + Drive sync + server restart ─────────────────
  // Spawns restart.py as a detached process so the MCP response is returned
  // BEFORE the server is killed. Server is back online in ~5-8 seconds.
  tool('xb_shutdown', {
    reason: z.string().optional().default('Manual shutdown')
              .describe('Reason for shutting down the node')
  }, async ({ reason }) => {
    log.info(`[xb_shutdown] Shutting down node gracefully. Reason: ${reason}`);

    // ── Flush all active session states to Drive before killing ──────────────
    // Prevents cookie loss when the operator shuts down while sessions are live.
    try {
      const { listActiveContexts, closeContext } = await import('./core/browser-pool.mjs');
      const active = listActiveContexts();
      if (active.length > 0) {
        log.info(`[xb_shutdown] Saving ${active.length} active session(s) before shutdown…`);
        // closeContext now saves state before closing — so this both saves and cleans up.
        await Promise.allSettled(active.map(sid => closeContext(sid)));
        log.info('[xb_shutdown] Sessions saved.');
      }
    } catch (saveErr) {
      log.warn(`[xb_shutdown] Pre-shutdown session save failed (non-fatal): ${saveErr.message}`);
    }

    // Push session JSONs and DB to Drive (detached, best-effort)
    try {
      const { spawn: _sp } = await import('node:child_process');
      const sc = '/content/xio-browser/colab/sync.py';
      if (fs.existsSync(sc)) {
        _sp('python3', [sc, '--push', '--what', 'sessions', '-q'], { detached: true, stdio: 'ignore' }).unref();
        _sp('python3', [sc, '--push', '--what', 'db',       '-q'], { detached: true, stdio: 'ignore' }).unref();
        log.info('[xb_shutdown] Triggered Drive push for sessions + DB.');
      }
    } catch (pushErr) {
      log.warn(`[xb_shutdown] Drive push trigger failed (non-fatal): ${pushErr.message}`);
    }

    // Unlock tailscale and session
    try {
      execSync('python3 /content/xio-browser/colab/boot.py --unlock-all', { stdio: 'ignore' });
    } catch (e) {
      log.error(`[xb_shutdown] Failed to release locks: ${e.message}`);
    }

    // Give sync processes a moment to start uploading, then kill
    setTimeout(() => {
      spawn('bash', ['-c', 'pkill -f boot.py; pkill -f xio-browser; pkill -f mcp-server'], {
        detached: true, stdio: 'ignore'
      });
    }, 3000);  // 3s instead of 1s — lets sync start its upload

    return {
      ok: true,
      status: 'shutting_down',
      message: 'Sessions saved, locks released. Shutting down in ~3s.'
    };
  }, { description: 'Gracefully shut down this Colab node: saves all active session states to Drive, releases Tailscale and session locks, then terminates the MCP server and all daemons. Use before Colab runtime disconnects to prevent orphaned locks and lost cookies.' });


  tool('xb_restart', {
    pull_code:         z.boolean().optional().default(true)
                         .describe('git pull latest code before restarting (default: true)'),
    delay_ms:          z.number().optional().default(800)
                         .describe('Milliseconds to wait before killing the server (default: 800)'),
    preserve_sessions: z.boolean().optional().default(false)
                         .describe('Skip Chrome Singleton lock cleanup so sessions survive the restart. Use when workflows are paused and must resume after restart.'),
    pause_jobs:        z.boolean().optional().default(false)
                         .describe('Signal running jobs to pause/checkpoint before killing the server. Requires xb_job_pause to be supported by the running job.'),
  }, async ({ pull_code, delay_ms, preserve_sessions, pause_jobs }) => {
    const restartScript = '/content/xio-browser/colab/restart.py';
    if (!fs.existsSync(restartScript)) {
      return {
        ok:    false,
        error: 'restart.py not found — ensure start.ipynb Cell 2 has been run first.',
      };
    }

    const tsIP = selfIP();
    const port = config.httpPort ?? 4242;

    log.info(`[xb_restart] Scheduling restart in ${delay_ms}ms (pull_code=${pull_code}, preserve_sessions=${preserve_sessions}, pause_jobs=${pause_jobs})…`);

    // Schedule detached restart — returns response before server dies
    setTimeout(() => {
      const scriptArgs = [
        restartScript,
        pull_code ? '--pull' : '--no-pull',
        '--delay', '0',           // restart.py built-in delay already applied via setTimeout
        ...(preserve_sessions ? ['--preserve-sessions'] : []),
        ...(pause_jobs        ? ['--pause-jobs']        : []),
      ];
      const child = spawn('python3', scriptArgs, {
        detached: true,           // survive parent process death
        stdio:    'ignore',
      });
      child.unref();              // don't wait for child — fire and forget
      log.info('[xb_restart] restart.py spawned (detached)');
    }, delay_ms);

    return {
      ok:                true,
      status:            'restarting',
      message:           `Server restart initiated. Will be back in ~8 seconds.`,
      pull_code,
      delay_ms,
      preserve_sessions,
      pause_jobs,
      // Poll this URL to know when the server is back
      health_url:        tsIP ? `http://${tsIP}:${port}/health`   : null,
      mcp_url:           tsIP ? `http://${tsIP}:${port}/mcp`      : null,
      events_url:        tsIP ? `http://${tsIP}:${port}/events`   : null,
      hint:              'Poll health_url every 2s until you get {"ok":true} — that means the server is back up.',
    };
  }, { description: 'Restart the xio-browser MCP server: optionally git-pulls latest code, syncs Drive state, and reboots the server process. Pass preserve_sessions=true to keep Chrome sessions alive across restart (needed for pause/resume). Returns before the server dies — poll health_url to detect when it is back up (~8s).' });

  log.info('MCP server created with all tools registered');
  return server;
}
