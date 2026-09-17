// ─── Job Manager ──────────────────────────────────────────────────────────
// Manages async workflow job execution.
// - Jobs run one at a time (single browser, single exit node at a time)
// - Each step in a workflow is captured as a screenshot + timing record
// - Screenshots saved locally AND to Drive/jobs/{job_id}/ in real time
// - Callers poll xb_job_poll to watch step-by-step progress

import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { createLogger } from '../utils/logger.mjs';
import { setExitNode, ensureDaemonRunning, selfIP } from '../utils/tailscale.mjs';
import { getContext, closeContext, evictIdleContexts } from './browser-pool.mjs';
import { saveStorageState, saveProfile, pushToStorage } from './session-manager.mjs';
import { runWorkflow } from './workflow-runner.mjs';
import { createJob, updateJobStatus,
         insertStep, getJob, getJobSteps, listJobs,
         upsertSession, upsertSessionService, getDb,
         getSessionExitBinding, bindSessionToExitNode,
         acquireSessionLock, releaseSessionLock } from './db.mjs';
import { Paths, dbFilePath, ensureDir } from '../utils/drive.mjs';

const log = createLogger('job-mgr');

// ── Runtime detection + concurrency limits ────────────────────────────────
// Detect GPU at import time (once per process)
let _runtimeType = 'cpu';
try { execSync('nvidia-smi', { timeout: 3000, stdio: 'ignore' }); _runtimeType = 'gpu'; } catch {}

const CONCURRENCY_LIMITS = {
  cpu: { headed: 3, headless: 6, appetize: 10 },
  gpu: { headed: 6, headless: 10, appetize: 15 },
};

export function getRuntimeType() { return _runtimeType; }
export function getConcurrencyLimits() { return CONCURRENCY_LIMITS[_runtimeType]; }

// ── Browser slot counters (tracked by type, not by job) ───────────────────
const _slotCounts = { headed: 0, headless: 0, appetize: 0 };

export function acquireSlot(type) {
  const limits = CONCURRENCY_LIMITS[_runtimeType];
  if (_slotCounts[type] >= limits[type]) {
    throw new Error(`BrowserSlotFull: ${type} at capacity (${_slotCounts[type]}/${limits[type]})`);
  }
  _slotCounts[type]++;
  log.info(`[slots] acquired ${type}: ${_slotCounts[type]}/${limits[type]}`);
}

export function releaseSlot(type) {
  if (_slotCounts[type] > 0) _slotCounts[type]--;
  log.info(`[slots] released ${type}: ${_slotCounts[type]}/${CONCURRENCY_LIMITS[_runtimeType][type]}`);
  // Drain pending queue after any slot frees up
  setImmediate(_tryStartNext);
}

export function getSlotCounts() {
  return { counts: { ..._slotCounts }, limits: CONCURRENCY_LIMITS[_runtimeType], runtimeType: _runtimeType };
}

// Cached Tailscale IP — never changes during runtime, don't execSync on every poll
let _cachedTsIP = null;
function getCachedTsIP() {
  if (!_cachedTsIP) _cachedTsIP = selfIP();
  return _cachedTsIP;
}

// Cancel-request Set — lets running workflows check ctx.isCancelled()
const _cancelRequested = new Set();

// ── Datetime-prefixed job directory names ──────────────────────────────────
// Format: {workflow-slug}_{YYYYMMDD}_{HHMMSS}_{8hex}  e.g. self-spawn_20260809_071750_60a641ab
// Consistent for ALL job dirs — top-level and inline sub-jobs alike.
// The jobId (8-char UUID prefix) is the DB primary key; all 8 chars go in the folder name.
const _jobDirCache = new Map(); // jobId → dirName (populated at enqueue time)

function jobDirName(jobId, workflowId) {
  const slug = (workflowId ?? 'job').replace(/[^a-zA-Z0-9-]/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  const now  = new Date();
  const ts   = now.toISOString().slice(0, 10).replace(/-/g, '') + '_' + now.toISOString().slice(11, 19).replace(/:/g, '');
  return `${slug}_${ts}_${jobId.slice(0, 8)}`;
}

function resolveJobDir(jobId) {
  if (_jobDirCache.has(jobId)) return _jobDirCache.get(jobId);
  // Scan for dir matching the full 8-char hex suffix
  try {
    const jobsBase = Paths.jobs();
    const hex8  = jobId.slice(0, 8);
    const match = fs.readdirSync(jobsBase).find(d => d.endsWith(hex8) || d.endsWith(jobId));
    if (match) { _jobDirCache.set(jobId, match); return match; }
  } catch { /* jobs dir may not exist yet */ }
  return jobId;
}

// Emit a server-sent event to connected SSE clients (if any)
function emit(type, data) {
  try { globalThis.__xioEmitEvent?.(type, data); } catch { /* SSE not started */ }
}

// ── Immediate DB flush to Drive ────────────────────────────────────────────
// Called after every job completes (done or error).
// Closes the 5-min keep-alive lag: session_valid flags + job records are
// durable immediately after each workflow, not just on the next tick.
// Uses better-sqlite3's WAL checkpoint to ensure data is on disk before copy.
function flushDbToDrive() {
  try {
    const db         = getDb();
    const localPath  = db.name;                      // e.g. /content/xio-browser.db
    const drivePath  = dbFilePath();                  // Drive-mounted mirror path

    if (!localPath || localPath === ':memory:') return;
    if (localPath === drivePath) return;             // same file — nothing to copy

    // Ensure parent directory exists (Drive path may not have been created yet)
    ensureDir(path.dirname(drivePath));
    // Guard: if drivePath was mistakenly created as a directory (EISDIR bug), remove it
    if (fs.existsSync(drivePath) && fs.statSync(drivePath).isDirectory()) {
      try { fs.rmdirSync(drivePath); } catch { /* non-fatal */ }
    }

    // WAL checkpoint: flush WAL into main DB file before copying
    db.pragma('wal_checkpoint(TRUNCATE)');

    // Atomic copy: write to .tmp first, then rename
    const tmpPath = drivePath + '.tmp';
    fs.copyFileSync(localPath, tmpPath);
    fs.renameSync(tmpPath, drivePath);

    log.info(`DB flushed to Drive: ${drivePath}`);
  } catch (e) {
    // Non-fatal: keep-alive will catch up on the next 5-min tick
    log.warn(`DB flush to Drive failed (non-fatal): ${e.message}`);
  }
}

/**
 * Immediately push a specific session JSON file to Drive after a job completes.
 * Fire-and-forget: spawned as a detached child process so it doesn't block
 * the job-manager loop. Keeps cookies safe even if the runtime crashes within
 * the 5-minute keep-alive sync window.
 * @param {string} sessionId
 */
function _pushSessionToDrive(sessionId) {
  if (!sessionId) return;
  const syncScript = '/content/xio-browser/colab/sync.py';
  if (!fs.existsSync(syncScript)) return; // not on Colab — no-op
  // Fire-and-forget IIFE — ES module compatible (no require)
  (async () => {
    try {
      const { spawn } = await import('node:child_process');
      const child = spawn('python3', [syncScript, '--push', '--what', 'sessions', '--session-id', sessionId], {
        detached: true, stdio: 'ignore',
      });
      child.unref(); // don't keep Node alive waiting for this
      log.info(`Targeted session push to Drive triggered (session=${sessionId})`);
    } catch (e) {
      log.warn(`Targeted session push to Drive failed (non-fatal): ${e.message}`);
    }
  })();
}

/**
 * Immediately push a specific session's Chrome profile archive to Drive after a job completes.
 * Fire-and-forget: spawned as a detached child process.
 * @param {string} sessionId
 */
function _pushProfileToDrive(sessionId) {
  if (!sessionId) return;
  const syncScript = '/content/xio-browser/colab/sync.py';
  if (!fs.existsSync(syncScript)) return; // not on Colab — no-op
  (async () => {
    try {
      const { spawn } = await import('node:child_process');
      const child = spawn('python3', [syncScript, '--push', '--what', 'chrome_profiles', '--session-id', sessionId], {
        detached: true, stdio: 'ignore',
      });
      child.unref();
      log.info(`Targeted profile push to Drive triggered (session=${sessionId})`);
    } catch (e) {
      log.warn(`Targeted profile push to Drive failed (non-fatal): ${e.message}`);
    }
  })();
}

// NOTE: _pushWorkflowToDrive removed — workflows are git-managed. Use xb_commit_patch + git push.

/**
 * Immediately push a job's result files to Drive after completion.
 * Always pushes: result.json + final phase screenshot.
 * On failure (success=false): pushes ALL screenshots for debugging.
 * Fire-and-forget: detached child process.
 * @param {string} dirName   - datetime-prefixed job directory name
 * @param {boolean} success  - determines how much to push
 */
function _pushJobToDrive(dirName, success) {
  const syncScript = '/content/xio-browser/colab/sync.py';
  if (!fs.existsSync(syncScript)) return;
  (async () => {
    try {
      const { spawn } = await import('node:child_process');
      const args = [syncScript, '--push', '--what', 'jobs', '--job-dir', dirName, '--exclude-steps'];
      if (!success) args.push('--full'); // push all screenshots for failures
      const child = spawn('python3', args, { detached: true, stdio: 'ignore' });
      child.unref();
      log.info(`Job push to Drive triggered (dir=${dirName}, full=${!success})`);
    } catch (e) {
      log.warn(`Job push to Drive failed (non-fatal): ${e.message}`);
    }
  })();
}

// ── Concurrent job pool ───────────────────────────────────────────────────
// Replaced the old single-job _running boolean with a pool.
// Multiple jobs run simultaneously — limited by browser slot counts (not job count).
const _activePool   = new Map();  // jobId → { jobId, workflowId, sessionId, slotType, startedAt }
const _pendingQueue = [];         // tasks waiting for a slot

// Currently executing jobs' live pages (for devtools MCP tools to attach to)
const _runningJobs = new Map();   // jobId → { jobId, sessionId, context, page }

// Backward compat: returns first active job (screencaster, devtools)
export function getRunningJob() {
  const first = _runningJobs.values().next().value;
  return first ?? null;
}

// Returns all running jobs (for multi-instance screencaster)
export function getRunningJobs() { return [..._runningJobs.values()]; }

// ── Paused-job registry ────────────────────────────────────────────────────
// jobId → { resolve, ctx, stepIndex, dirName, sessionId } — set by ctx.pause()
const _pausedJobs = new Map();

/** Returns the paused job registry entry for a given job ID (or null). */
export function getPausedJob(jobId) { return _pausedJobs.get(jobId) ?? null; }

/** List all paused jobs. */
export function listPausedJobs() {
  return [..._pausedJobs.entries()].map(([id, e]) => ({
    job_id:     id,
    paused_at:  e.pausedAt,
    step_index: e.stepIndex,
    reason:     e.reason,
    dir_name:   e.dirName,
    session_id: e.sessionId,
  }));
}

/** Resume a paused job by providing the resolve function stored in _pausedJobs. */
export function resumePausedJob(jobId) {
  const entry = _pausedJobs.get(jobId);
  if (!entry) return { ok: false, error: `No paused job found: ${jobId}` };
  _pausedJobs.delete(jobId);
  updateJobStatus(jobId, 'running');
  entry.resolve(); // unblocks the await in ctx.pause()
  log.info(`[job-mgr] Resumed paused job ${jobId}`);
  return { ok: true, job_id: jobId };
}



/**
 * Enqueue a workflow job.
 * Returns immediately with the job_id.
 * If session_id is bound to a different exit node and force is not set,
 * returns an error object instead of a job_id.
 *
 * @param {string}  workflowId
 * @param {string}  [sessionId]
 * @param {string}  [exitNode]
 * @param {object}  [params]
 * @param {boolean} [force=false]  - override session exit-node binding mismatch
 * @param {string}  [slotType]     - 'headed'|'headless'|'appetize' (default: auto-detect from workflow meta)
 * @param {boolean} [enableLogging=false]   - enable job step logging/screenshots
 * @param {boolean} [enableStreaming=false]  - enable SSE streaming of job events
 */
export function enqueueJob({ workflowId, sessionId, exitNode, params, force = false, parentJobId = null,
                             slotType, enableLogging = false, enableStreaming = false }) {
  // ── Session ↔ Exit Node Binding Check ──────────────────────────────
  let bindingWarning = null;
  if (sessionId && exitNode) {
    const binding = getSessionExitBinding(sessionId);
    if (binding && binding.exit_node && binding.exit_node !== exitNode) {
      // MISMATCH — different exit node requested for a bound session
      const msg = [
        `⚠️  CRITICAL EXIT-NODE MISMATCH ⚠️`,
        `Session "${sessionId}" is permanently bound to exit node ${binding.exit_node}`,
        `(bound on ${binding.bound_at}).`,
        `You requested exit node: ${exitNode}`,
        ``,
        `WHY THIS IS DANGEROUS:`,
        `  • The browser fingerprint (User-Agent, WebGL, timezone, screen) was built`,
        `    for the ${binding.exit_node} device. Switching exit nodes creates a`,
        `    contradictory fingerprint that Google and other services can detect.`,
        `  • Stored cookies are tied to the original device identity. Replaying them`,
        `    from a different IP + fingerprint triggers account protection checks.`,
        `  • In worst case: account suspension or forced re-authentication.`,
        ``,
        `TO PROCEED ANYWAY (AT YOUR OWN RISK):`,
        `  Re-run with force=true in your xb_run_workflow call.`,
        `  This will log a CRITICAL_OVERRIDE_ACTIVE marker in the job result.`,
      ].join('\n');

      if (!force) {
        log.warn(`Blocking job: session "${sessionId}" exit-node mismatch (bound=${binding.exit_node}, requested=${exitNode})`);
        // Return a sentinel — caller checks for .binding_error
        return {
          binding_error: true,
          session_id:    sessionId,
          bound_exit_node:    binding.exit_node,
          requested_exit_node: exitNode,
          message: msg,
        };
      }

      // force=true — log override and continue with a warning attached to the job
      log.warn(`CRITICAL_OVERRIDE: session "${sessionId}" running on non-bound exit node (force=true)`);
      bindingWarning = {
        override: true,
        bound_exit_node:    binding.exit_node,
        requested_exit_node: exitNode,
        message: 'CRITICAL_OVERRIDE_ACTIVE: session ran on non-bound exit node. Fingerprint may be inconsistent.',
      };
    }
  }

  // Auto-detect slot type from workflow metadata if not explicitly provided
  const resolvedSlotType = slotType ?? _inferSlotType(workflowId);

  const jobId   = randomUUID().split('-')[0]; // short 8-char ID
  const parentDirName = parentJobId ? resolveJobDir(parentJobId) : null;
  const dirName = jobDirName(jobId, workflowId, parentDirName);
  _jobDirCache.set(jobId, dirName);           // cache for this runtime
  const job   = {
    id:          jobId,
    workflow_id: workflowId,
    session_id:  sessionId ?? null,
    exit_node:   exitNode,
    status:      'pending',
  };

  createJob(job);
  _pendingQueue.push({
    jobId, dirName, workflowId, sessionId, exitNode, params,
    bindingWarning, parentJobId, slotType: resolvedSlotType,
    enableLogging, enableStreaming,
  });
  log.info(`Job enqueued: ${jobId} (${workflowId}) — queue depth: ${_pendingQueue.length}, slot: ${resolvedSlotType}`);

  // Kick off queue processing in next tick
  setImmediate(_tryStartNext);

  return jobId;
}

/**
 * Infer the browser slot type from workflow metadata.
 * Checks for `meta.headed`, `meta.appetize`, or defaults to 'headless'.
 */
function _inferSlotType(workflowId) {
  try {
    const { getWorkflowMeta } = require ? {} : {}; // avoid circular — use dynamic import below
    // Quick check: if workflow file has `appetize` in name → appetize slot
    if (/appetize|broker/i.test(workflowId)) return 'appetize';
    // Default to headless (most workflows)
    return process.env.DISPLAY ? 'headed' : 'headless';
  } catch {
    return 'headless';
  }
}

export function pollJob(jobId) {
  const job   = getJob(jobId);
  if (!job) return null;
  const steps   = getJobSteps(jobId);
  const tsIP    = getCachedTsIP();
  const httpPort = globalThis.__xioHttpPort ?? 4242;
  const dirName  = resolveJobDir(jobId); // datetime-prefixed dir name

  return {
    job_id:        job.id,
    workflow_id:   job.workflow_id,
    session_id:    job.session_id,
    exit_node:     job.exit_node,
    status:        job.status,
    created_at:    job.created_at,
    started_at:    job.started_at,
    completed_at:  job.completed_at,
    result:        job.result ? JSON.parse(job.result) : null,
    error:         job.error,
    dir_name:      dirName,
    // Convenience: URL to browse all job files (screenshots + result.json)
    files_url:     tsIP ? `http://${tsIP}:${httpPort}/jobs/${dirName}` : null,
    steps: steps.map(s => ({
      index:          s.step_index,
      name:           s.step_name,
      status:         s.status,
      duration_ms:    s.duration_ms,
      logs:           s.log_lines ? JSON.parse(s.log_lines) : [],
      // Return URL instead of inline base64 — avoids bloating LLM context windows
      screenshot_url: tsIP && s.screenshot
        ? `http://${tsIP}:${httpPort}/jobs/${dirName}/${String(s.step_index).padStart(2,'0')}_${s.step_name}.jpg`
        : null,
      // screenshot column in DB is now null (file is on disk) — field kept for API compat
      screenshot: null,
    })),
  };
}

export function cancelJob(jobId) {
  const idx = _pendingQueue.findIndex(j => j.jobId === jobId);
  if (idx !== -1) {
    _pendingQueue.splice(idx, 1);
    updateJobStatus(jobId, 'cancelled');
    log.info(`Job cancelled from queue: ${jobId}`);
    return true;
  }
  // Signal the running job to stop at the next isCancelled() check
  _cancelRequested.add(jobId);
  updateJobStatus(jobId, 'cancelling');
  log.info(`Job cancellation requested: ${jobId} (will stop at next checkpoint)`);

  // KEY FIX: if job is paused (waiting for HITL resume), auto-resume it so the
  // isCancelled() flag is actually checked and the pool slot is eventually freed.
  if (_pausedJobs.has(jobId)) {
    log.info(`[job-mgr] cancelJob: job ${jobId} is paused — auto-resuming to unblock runner`);
    resumePausedJob(jobId);  // unblocks the pause Promise; job will detect isCancelled() and abort
  }

  return false;
}

/**
 * Emergency escape hatch — use after HITL release when the job runner is stuck.
 *
 * Safely releases any paused jobs (by resuming+cancelling them), then waits up to
 * `timeoutMs` for active pool to drain. If still stuck, force-clears the pool
 * and kicks the queue. Sessions are 100% safe: this never touches the browser context.
 *
 * @param {number} timeoutMs  Grace period before force-reset (default 6000ms)
 */
export async function forceResetRunner(timeoutMs = 6000) {
  log.warn('[job-mgr] forceResetRunner called — releasing stuck job runner');
  const results = { paused_released: [], queue_cleared: [], force_reset: false };

  // 1. Release all paused jobs
  for (const [jobId] of [..._pausedJobs]) {
    _cancelRequested.add(jobId);
    resumePausedJob(jobId);         // unblocks Promise; job will abort via isCancelled()
    results.paused_released.push(jobId);
    log.info(`[job-mgr] forceResetRunner: released paused job ${jobId}`);
  }

  // 2. Cancel any queued pending jobs that will never start
  //    (don't clear them automatically — let caller decide)

  // 3. Wait for active pool to drain naturally (jobs may need a tick to reach finally block)
  if (_activePool.size > 0) {
    const deadline = Date.now() + timeoutMs;
    while (_activePool.size > 0 && Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 250));
    }
  }

  // 4. Nuclear option: if still stuck, force-clear and kick the queue
  if (_activePool.size > 0) {
    log.warn(`[job-mgr] forceResetRunner: ${_activePool.size} jobs still active after grace period — force-clearing`);
    _activePool.clear();
    _runningJobs.clear();
    // Reset slot counts (best-effort — may be slightly off but self-corrects on next job)
    _slotCounts.headed = 0;
    _slotCounts.headless = 0;
    _slotCounts.appetize = 0;
    results.force_reset = true;
    if (_pendingQueue.length > 0) setImmediate(_tryStartNext);
  }

  log.info('[job-mgr] forceResetRunner complete', results);
  return results;
}

export function listAllJobs(opts) {
  return listJobs(opts);
}


// ── Concurrent Queue Processing ───────────────────────────────────────────

/**
 * Try to start as many pending jobs as slots allow.
 * Called on enqueue, on slot release, and after any job finishes.
 */
function _tryStartNext() {
  while (_pendingQueue.length > 0) {
    const task = _pendingQueue[0];
    const type = task.slotType ?? 'headless';

    // Check if a slot is available
    const limits = CONCURRENCY_LIMITS[_runtimeType];
    if (_slotCounts[type] >= limits[type]) {
      log.info(`[pool] ${type} at capacity (${_slotCounts[type]}/${limits[type]}) — ${_pendingQueue.length} job(s) waiting`);
      break; // wait for releaseSlot() to call _tryStartNext() again
    }

    // Slot available — start this job
    _pendingQueue.shift();
    _slotCounts[type]++;
    _activePool.set(task.jobId, {
      jobId: task.jobId,
      workflowId: task.workflowId,
      sessionId: task.sessionId,
      slotType: type,
      startedAt: Date.now(),
    });
    log.info(`[pool] Starting job ${task.jobId} (${task.workflowId}) — slot ${type}: ${_slotCounts[type]}/${limits[type]}, active: ${_activePool.size}`);

    // Run concurrently — each job manages its own slot release
    executeJob(task).catch(e => {
      log.error(`Job ${task.jobId} crashed: ${e.message}`);
      log.error(e.stack ?? '(no stack)');
      updateJobStatus(task.jobId, 'error', null, e.message);
    }).finally(() => {
      _activePool.delete(task.jobId);
      _runningJobs.delete(task.jobId);
      if (_slotCounts[type] > 0) _slotCounts[type]--;
      log.info(`[pool] Job ${task.jobId} finished — slot ${type}: ${_slotCounts[type]}/${limits[type]}, active: ${_activePool.size}`);
      _cancelRequested.delete(task.jobId);
      // Try to start more jobs now that a slot freed up
      setImmediate(_tryStartNext);
    });
  }
}

// ── Job Execution ──────────────────────────────────────────────────────────

async function executeJob({ jobId, dirName, workflowId, sessionId, exitNode, params, bindingWarning, parentJobId,
                            enableLogging = false, enableStreaming = false }) {
  log.info(`Executing job ${jobId}: workflow=${workflowId} exit=${exitNode} logging=${enableLogging} streaming=${enableStreaming}`);
  updateJobStatus(jobId, 'running');
  if (enableStreaming) emit('job.started', { job_id: jobId, workflow_id: workflowId, session_id: sessionId });

  let jobDriveDir;
  if (parentJobId) {
    const parentDir = path.join(Paths.jobs(), resolveJobDir(parentJobId));
    jobDriveDir = path.join(parentDir, dirName);
  } else {
    jobDriveDir = path.join(Paths.jobs(), dirName);
  }
  ensureDir(jobDriveDir);

  // Ensure Tailscale is healthy
  ensureDaemonRunning();

  // Set exit node (routes all browser traffic through caller's residential IP)
  await setExitNode(exitNode);

  // Auto-ensure session row exists in DB (satisfies FK constraints in session_services)
  // Use display_name=null so upsertSession preserves any existing email-form display_name
  // rather than overwriting it with the bare slug.
  if (sessionId) {
    try { upsertSession({ id: sessionId, display_name: null }); } catch {}
    // First-use binding: lock this session to this exit node permanently
    if (exitNode) {
      bindSessionToExitNode(sessionId, exitNode);
      log.info(`Session "${sessionId}" bound to exit node ${exitNode} (first-use lock)`);
    }
  }

  // ── Distributed session lock (top-level jobs only, not sub-workflows) ──────
  // Prevents two runtimes from simultaneously writing the same profile tar.gz
  // or racing on state.json saves. Advisory: if Supabase is unreachable or lock
  // can't be acquired, we warn and proceed rather than block indefinitely.
  const _nodeName = process.env.XIO_NODE_NAME ?? 'colab-master';
  let _sessionLockAcquired = false;
  if (sessionId && !parentJobId) {
    try {
      // TTL = 25 min — covers typical job timeout; crashed nodes auto-release
      _sessionLockAcquired = await acquireSessionLock(sessionId, _nodeName, 25 * 60 * 1000);
      if (!_sessionLockAcquired) {
        log.warn(`[job-mgr] Session ${sessionId} is locked by another runtime — proceeding without lock (advisory)`);
      } else {
        log.info(`[job-mgr] Session lock acquired: ${sessionId} (node=${_nodeName})`);
      }
    } catch (lockErr) {
      log.warn(`[job-mgr] acquireSessionLock failed (${lockErr.message}) — proceeding unlocked`);
    }
  }

  // On-demand pull: ensure session JSON + Chrome profile are on disk before launching browser.
  // This is a no-op if the files already exist; sync.py handles deduplication.
  if (sessionId) {
    const { ensureSessionState } = await import('./session-manager.mjs');
    await ensureSessionState(sessionId).catch(e =>
      log.warn(`[job-mgr] ensureSessionState failed for ${sessionId}: ${e.message}`)
    );
  }

  let page = null;
  let context = null;
  let stepIndex = 0;
  let finalResult = null;
  let _currentStepLogs = null; // points to the active step's log array
  const logs = [];

  try {
    // Build browser context (with fingerprint morphed to exit node's OS)
    // NOTE: anon-${jobId} is a throwaway context — mark ephemeral:true so
    // session-manager skips serial assignment and sync.py skips Drive push.
    const _resolvedSessionId = sessionId ?? `anon-${jobId}`;
    const _isAnon = !sessionId;
    const ctxEntry = await getContext(_resolvedSessionId, exitNode, { ephemeral: _isAnon });
    context = ctxEntry.context;
    // One-shot retry: if newPage() fails (stale context after restart), get a fresh context
    page = await context.newPage().catch(async (err) => {
      log.warn(`[job-manager] newPage() failed (${err.message.slice(0, 80)}) — retrying with fresh context`);
      const fresh = await getContext(_resolvedSessionId, exitNode, { forceNew: true, ephemeral: _isAnon });
      context = fresh.context;
      return context.newPage();
    });

    // Expose live page for devtools MCP tools (concurrent — each job tracked separately)
    _runningJobs.set(jobId, { jobId, sessionId, context, page });

    // ── Build ctx object passed to workflow ──────────────────────────────
    const ctx = {
      page,
      context,
      sessionId,            // Fix #5: workflows read ctx.sessionId for per-slot persistence
      jobId,                // Workflows can use this to build per-job paths (screenshots, etc.)
      dirName: resolveJobDir(jobId), // leaf dir name (may differ from jobDriveDir for nested sub-workflows)
      jobDir:  jobDriveDir,          // full absolute path — correct even for nested sub-workflow dirs
      isCancelled: () => _cancelRequested.has(jobId),  // Fix #13: cooperative cancel

      /** Execute a named step, auto-screenshot after.
       *
       * @param {string}   name  - Step label (used in DB + filenames)
       * @param {Function} fn    - Async step body
       * @param {object}   [opts]
       * @param {boolean}  [opts.throwOnFail=true]   - Rethrow on error (set false for optional steps)
       * @param {boolean}  [opts.hitlOnFail=false]   - Pause job via ctx.hitl() on error instead of failing
       * @param {string}   [opts.hitlMessage]         - Custom HITL message (defaults to error message)
       * @param {string}   [opts.hitlInstructions]    - Instructions shown in HITL notice
       * @param {boolean}  [opts.autoScreenshot=true] - Take patchright auto-snap after step completes.
       *                                                Set false when the step manages its own screenshots
       *                                                (e.g. stealth sidecar steps, multi-shot steps).
       */
      async step(name, fn, opts = {}) {
        // ── Zero-overhead path: no logging, no screenshots, no DB ─────────
        if (!enableLogging) {
          try { await fn(); } catch (err) {
            if (opts.throwOnFail !== false) throw err;
            logs.push(`[job-manager] ⚠️ step "${name}" failed (non-fatal): ${err.message}`);
          }
          return;
        }
        // ── Full logging path ─────────────────────────────────────────────
        const { throwOnFail = true, hitlOnFail = false, hitlMessage, hitlInstructions, autoScreenshot = true } = opts;
        const si    = stepIndex++;
        const start = Date.now();
        const stepLogs = [];
        _currentStepLogs = stepLogs; // wire ctx.log() into this step

        log.info(`  step[${si}] ${name}`);
        insertStep({
          job_id: jobId, step_index: si, step_name: name,
          status: 'running', screenshot: null, log_lines: '[]', duration_ms: null,
        });

        let screenshotB64 = null;
        try {
          await fn();
          // Auto-snapshot of the patchright page after step completes.
          // Skipped when autoScreenshot:false (step manages its own captures,
          // e.g. stealth sidecar steps that write to screenshotDir directly).
          if (autoScreenshot) {
            const buf = await page.screenshot(
              { type: 'jpeg', quality: 70, fullPage: false, timeout: 8000 }
            ).catch(() => null);
            if (buf) {
              const stepsDir = path.join(jobDriveDir, 'steps');
              fs.mkdirSync(stepsDir, { recursive: true });
              const imgPath = path.join(stepsDir, `${String(si).padStart(2, '0')}_${name}.jpg`);
              fs.writeFileSync(imgPath, buf);
            }
          }

          // Update step as done (screenshot column stays null — file is on disk)
          getDb().prepare(`
            UPDATE job_steps SET
              status = 'done', screenshot = NULL, log_lines = ?, duration_ms = ?
            WHERE job_id = ? AND step_index = ?
          `).run(JSON.stringify(stepLogs), Date.now() - start, jobId, si);
          _currentStepLogs = null; // step done — detach so post-step logs go to global only
          if (enableStreaming) emit('job.step.done', { job_id: jobId, step: si, name, duration_ms: Date.now() - start });

        } catch (err) {
          // Screenshot on error too
          // Error screenshot — same JPEG q70 compression
          const errBuf = await page.screenshot(
            { type: 'jpeg', quality: 70, fullPage: false, timeout: 8000 }
          ).catch(() => null);
          if (errBuf) {
            // Error screenshots to disk only, not SQLite
            const stepsDir = path.join(jobDriveDir, 'steps');
            fs.mkdirSync(stepsDir, { recursive: true });
            fs.writeFileSync(path.join(stepsDir, `${String(si).padStart(2, '0')}_${name}_ERROR.jpg`), errBuf);
          }
          getDb().prepare(`
            UPDATE job_steps SET
              status = 'error', screenshot = NULL, log_lines = ?, duration_ms = ?
            WHERE job_id = ? AND step_index = ?
          `).run(JSON.stringify([err.message, ...stepLogs]), Date.now() - start, jobId, si);
          _currentStepLogs = null; // step errored — detach so post-error logs go to global only
          if (enableStreaming) emit('job.step.error', { job_id: jobId, step: si, name, error: err.message });

          // ── HITL escalation (hitlOnFail option) ─────────────────────────────
          // If the step was configured with hitlOnFail:true, pause the job for
          // human intervention instead of hard-failing. Any workflow can opt-in:
          //   await ctx.step('my_step', fn, { hitlOnFail: true, hitlMessage: '...' });
          if (hitlOnFail && typeof ctx.hitl === 'function') {
            const _msg = hitlMessage ?? `Step "${name}" failed: ${err.message}`;
            const _instr = hitlInstructions ?? `Investigate the "${name}" step failure (check screenshots in the job folder), resolve the issue, then resume the job.`;
            logs.push(`[job-manager] ⏸ hitlOnFail — pausing job for human intervention`);
            await ctx.hitl(_msg, { instructions: _instr });
            return; // after HITL resume, continue workflow execution
          }

          // Non-fatal step (throwOnFail: false) — log and continue
          if (!throwOnFail) {
            logs.push(`[job-manager] ⚠️ step "${name}" failed (non-fatal): ${err.message}`);
            return;
          }

          throw err; // propagate to abort workflow

        }
      },

      /** Extra manual screenshot mid-step — JPEG q70 */
      async screenshot(label = 'manual') {
        const buf = await page.screenshot(
          { type: 'jpeg', quality: 70, fullPage: false, timeout: 8000 }
        ).catch(() => null);
        if (!buf) return null;
        const stepsDir = path.join(jobDriveDir, 'steps');
        fs.mkdirSync(stepsDir, { recursive: true });
        const imgPath = path.join(stepsDir, `${String(stepIndex).padStart(2, '0')}_${label}.jpg`);
        fs.writeFileSync(imgPath, buf);
        return `data:image/jpeg;base64,${buf.toString('base64')}`;
      },

      /** Append a text log line (goes to global job log AND current step log) */
      log(msg) {
        // Strip ANSI escape codes (color/bold/etc from subprocess output like UC Chrome)
        // These control characters break JSON serialization in SQLite and the poll endpoint.
        const stripped = String(msg).replace(/\x1b\[[0-9;]*[mABCDEFGHJKSTfihnrsu]/g, '');
        // Truncate any URL token longer than 120 chars to keep logs readable
        const clean = stripped.replace(/https?:\/\/\S{120,}/g,
          m => m.slice(0, 80) + '\u2026[url truncated]');
        logs.push(clean);
        log.info(`  [workflow] ${clean}`);
        // Also write to current step log if a step is active
        if (_currentStepLogs) _currentStepLogs.push(clean);
      },


      /** Set the final result returned to the caller */
      setResult(data) {
        finalResult = data;
      },

      /**
       * Run another workflow INLINE (same process, no job queue) as a nested
       * sub-workflow. Creates a proper sub-dir inside the parent job dir with
       * its own steps/ and result.json.
       *
       * Usage (from any workflow):
       *   const r = await ctx.runInline('google-signin', sessionId);
       *   const r = await ctx.runInline('tailscale-auth', sessionId, { extra: 'param' });
       *
       * This avoids potential deadlocks when a running job tries
       * to enqueue a child job via the HTTP API (slot contention).
       *
       * @param {string}  workflowName  - workflow filename without .mjs extension
       * @param {string}  [subSessionId] - session_id for sub-workflow (defaults to parent's)
       * @param {object}  [params]       - extra params forwarded to sub-workflow run()
       * @returns {{ ok: boolean, result: any, error?: string }}
       */
      async runInline(workflowName, subSessionId, params = {}, _parentDir = jobDriveDir) {
        const _xiobr = '/content/xio-browser';

        // ── Sub-job dir: {workflowName}_{YYYYMMDD}_{HHMMSS}_{8hex}/ directly inside parent job dir ──
        // Same format as top-level jobs. No intermediate "sub-jobs/" directory.
        const _subNow    = new Date();
        const _subDt     = _subNow.toISOString().slice(0, 10).replace(/-/g, '') + '_' + _subNow.toISOString().slice(11, 19).replace(/:/g, '');
        const _subId     = randomUUID().replace(/-/g, '').slice(0, 8); // 8-char hex id (matches top-level job ID length)
        const _subSlug   = workflowName.replace(/[^a-zA-Z0-9-]/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
        const subDirSlug = `${_subSlug}_${_subDt}_${_subId}`;
        const subJobDir  = path.join(_parentDir, subDirSlug); // inside current workflow's dir
        fs.mkdirSync(`${subJobDir}/steps`, { recursive: true });

        ctx.log(`[runInline] Starting '${workflowName}' → ${subDirSlug}`);

        // ── Isolated browser context for cross-session sub-workflows ──────────
        // If subSessionId differs from the parent session, open a new page in the
        // sub-session's browser context so the parent ctx.page is never navigated
        // away (e.g. self-spawn's worker notebook page stays intact during
        // tailscale-signin/tailscale-auth sub-workflows).
        let subPage    = ctx.page;
        let subContext = ctx.context;
        let _ownedPage = null;
        if (subSessionId && subSessionId !== ctx.sessionId) {
          try {
            // getContext() returns { context, profile, exitNodeIP } — must unwrap .context
            // Also pass exitNode so the fingerprint matches the current session's exit node.
            const _subEntry = await getContext(subSessionId, exitNode);
            subContext = _subEntry.context;
            subPage    = await subContext.newPage();
            _ownedPage = subPage;
            ctx.log(`[runInline] Isolated context for session '${subSessionId}' — parent page preserved`);
          } catch (e) {
            ctx.log(`[runInline] Isolated context failed for '${subSessionId}': ${e.message} — sharing parent ctx.page`);
          }
        }

        // Sub-ctx mirrors the parent ctx API but scoped to the sub-dir
        let _subStepIdx = 0;
        const subCtx = {
          page:        subPage,
          context:     subContext,
          sessionId:   subSessionId ?? ctx.sessionId,
          jobId:       `${jobId}_${workflowName}`,
          // dirName updated for nested runInline: lets grandchild sub-workflows
          // also create sub-job dirs relative to this sub-job (no sub-jobs/ prefix)
          dirName:     `${ctx.dirName ?? jobId}/${subDirSlug}`,
          jobDir:      subJobDir,
          isCancelled: ctx.isCancelled,
          _result:     null,
          _hitlPending: null,

          log: (msg) => ctx.log(`[${workflowName}] ${msg}`),

          setResult: (r) => { subCtx._result = r; },

          // Sub-workflow steps write screenshots to sub-jobs/{wf}/steps/, NOT parent's steps/
          step: async (name, fn, _opts = {}) => {
            const si = _subStepIdx++;
            subCtx.log(`step[${si}] ${name}`);
            try {
              await fn();
              // Capture screenshot of current sub-page state into sub-dir
              if (subCtx.page) {
                const buf = await subCtx.page.screenshot(
                  { type: 'jpeg', quality: 70, fullPage: false, timeout: 8000 }
                ).catch(() => null);
                if (buf) {
                  fs.writeFileSync(
                    path.join(subJobDir, 'steps', `${String(si).padStart(2, '0')}_${name}.jpg`),
                    buf
                  );
                }
              }
              subCtx.log(`✅ step "${name}" done`);
            } catch (e) {
              subCtx.log(`❌ step "${name}" error: ${e.message}`);
              if (!(_opts.throwOnFail === false)) throw e;
            }
          },

          // Delegate screenshot/pause/hitl to parent ctx so HITL/pause still works
          screenshot: ctx.screenshot?.bind(ctx),
          pause:      ctx.pause?.bind(ctx),
          hitl:       ctx.hitl?.bind(ctx),

          // runInline is recursive — sub-workflows can inline their own children.
          // Each level passes subJobDir as _parentDir so grandchild sub-workflows
          // are created inside the current sub-workflow's dir, not the top-level job dir.
          runInline:  (wfName, ssId, p = {}) => ctx.runInline(wfName, ssId, p, subJobDir),
        };

        // Run the target workflow
        try {
          const { run } = await import(`file://${_xiobr}/workflows/${workflowName}.mjs`);
          await run(subCtx, { session_id: subCtx.sessionId, ...params });
          fs.writeFileSync(
            path.join(subJobDir, 'result.json'),
            JSON.stringify({ ok: true, result: subCtx._result, ts: new Date().toISOString() }, null, 2)
          );
          ctx.log(`[runInline] '${workflowName}' completed ok`);
          return { ok: true, result: subCtx._result };
        } catch (e) {
          ctx.log(`[runInline] '${workflowName}' error: ${e.message}`);
          fs.writeFileSync(
            path.join(subJobDir, 'result.json'),
            JSON.stringify({ ok: false, error: e.message, ts: new Date().toISOString() }, null, 2)
          );
          return { ok: false, error: e.message };
        } finally {
          // Close only the page we opened for the isolated context.
          // The context itself stays in the browser-pool for re-use.
          if (_ownedPage) await _ownedPage.close().catch(() => {});
        }
      },

      /**
       * Pause the current workflow and wait for agent to resume it.
       * Writes a checkpoint file and blocks until resumePausedJob(jobId) is called.
       * @param {string} reason - human-readable reason for pausing
       */
      async pause(reason = 'Agent pause requested') {
        ctx.log(`⏸  Pausing job ${jobId}: ${reason}`);
        updateJobStatus(jobId, 'paused');

        // Write checkpoint to disk so the state is durable across memory loss
        const checkpoint = {
          job_id:     jobId,
          step_index: stepIndex,
          paused_at:  new Date().toISOString(),
          reason,
          logs,
        };
        fs.writeFileSync(
          path.join(jobDriveDir, 'checkpoint.json'),
          JSON.stringify(checkpoint, null, 2)
        );

        // Emit SSE so the agent is notified immediately
        if (enableStreaming) emit('job.paused', { job_id: jobId, step_index: stepIndex, reason });

        // Block here until resume is called
        await new Promise(resolve => {
          _pausedJobs.set(jobId, {
            resolve,
            stepIndex,
            reason,
            dirName,
            sessionId,
            pausedAt: new Date().toISOString(),
          });
        });

        ctx.log(`▶️  Job ${jobId} resumed at step ${stepIndex}`);
        if (enableStreaming) emit('job.resumed', { job_id: jobId, step_index: stepIndex });
      },

      /**
       * Human-in-the-loop pause.
       * Structured wrapper around ctx.pause() — stores the operator message and
       * instructions alongside the checkpoint so the agent/operator knows WHY the
       * job paused. Also writes hitl.json to the job folder for Drive visibility.
       *
       * @param {string} message       - Short reason for the HITL pause
       * @param {object} [opts]
       * @param {string} [opts.instructions] - Detailed instructions for the operator
       * @param {string} [opts.stepName]     - Step name (informational)
       * @param {string} [opts.errorType]    - Machine-readable error type
       */
      async hitl(message, opts = {}) {
        const { instructions, stepName, errorType, ...extra } = opts;
        const hitlData = {
          job_id:       jobId,
          step_index:   stepIndex,
          paused_at:    new Date().toISOString(),
          message,
          instructions: instructions ?? null,
          step_name:    stepName ?? null,
          error_type:   errorType ?? null,
          extra,
        };
        // Write hitl.json for Drive/operator visibility
        try {
          fs.writeFileSync(
            path.join(ctx.paths.dir, 'hitl_notice.json'),
            JSON.stringify(hitlData, null, 2)
          );
        } catch (_) { /* non-fatal */ }
        ctx.log(`⏸  HITL: ${message}`);
        if (instructions) ctx.log(`  Instructions: ${instructions.slice(0, 200)}`);
        // Delegate to ctx.pause() which handles the actual blocking + resume
        return ctx.pause(message);
      },
    };

    // ── Run the workflow ─────────────────────────────────────────────────
    await runWorkflow(workflowId, ctx, params);

    // ── Persist storageState after successful run ─────────────────────────
    // Saves ALL domain cookies (Google + V0 + anything else) in one blob.
    // Also auto-detects which services are now logged in from the cookie domains.
    if (sessionId) {
      try {
        const storageState = await context.storageState();
        saveStorageState(sessionId, storageState);
        // Save full Chrome profile to Drive (non-blocking, best-effort)
        try { saveProfile(sessionId); } catch (e) { log.warn(`Profile save failed: ${e.message}`); }

        // Auto-record active services from cookie domains
        const DOMAIN_TO_SERVICE = {
          'google.com':           'google',
          'accounts.google.com':  'google',
          'v0.dev':               'v0',
          'vercel.com':           'vercel',
          'github.com':           'github',
          'gitlab.com':           'gitlab',
          'twitter.com':          'twitter',
          'x.com':                'twitter',
          'proton.me':            'proton',
          'protonmail.com':       'proton',
        };
        const detectedServices = new Set();
        for (const cookie of (storageState.cookies ?? [])) {
          const domain = cookie.domain.replace(/^\./, '');
          const svc = Object.entries(DOMAIN_TO_SERVICE).find(([d]) => domain.endsWith(d))?.[1];
          if (svc) detectedServices.add(svc);
        }
        for (const svc of detectedServices) {
          try {
            upsertSessionService({ session_id: sessionId, service: svc, session_valid: true });
            log.info(`  Auto-recorded service: ${svc} in slot ${sessionId}`);
          } catch (fkErr) {
            log.warn(`  Could not record service ${svc} (session not in DB yet): ${fkErr.message}`);
          }
        }
      } catch (persistErr) {
        log.warn(`  Session persistence warning (non-fatal): ${persistErr.message}`);
      }
    }

    // Write result summary to Drive
    const _jobRow = getJob(jobId);
    const resultPayload = {
      job_id:      jobId,
      workflow_id: workflowId,
      session_id:  sessionId,
      status:      'done',
      started_at:  _jobRow?.started_at,
      completed_at: new Date().toISOString(),
      result:      finalResult,
      logs,
      // Propagate binding override warning if this job forced a mismatch
      ...(bindingWarning ? { binding_warning: bindingWarning } : {}),
    };
    const resultPath = path.join(jobDriveDir, 'result.json');
    fs.writeFileSync(resultPath, JSON.stringify(resultPayload, null, 2));

    updateJobStatus(jobId, 'done', resultPayload);
    // Clean up checkpoint.json if it exists (written during pause, stale after completion)
    try {
      const ckpt = path.join(jobDriveDir, 'checkpoint.json');
      if (fs.existsSync(ckpt)) fs.unlinkSync(ckpt);
    } catch {}
    flushDbToDrive();
    // Generalised post-job push: session JSON -> Supabase (primary, awaited) + Drive (bg)
    // Chrome profile -> R2 (primary, awaited) + Drive (bg). Replaces the old Drive-only helpers.
    if (sessionId) {
      pushToStorage(sessionId, {
        session: true,
        profile: true,
        awaitPrimary: false, // fire-and-forget — job is done, don't hold the queue
        log: msg => log.info(msg),
      }).catch(e => log.warn(`[job-mgr] post-job push failed (non-fatal): ${e.message}`));
    }
    _pushJobToDrive(dirName, true);  // push result.json + final screenshot to Drive
    log.info(`Job ${jobId} completed successfully`);
    if (enableStreaming) emit('job.done', { job_id: jobId, result: finalResult });


  } catch (err) {
    log.error(`Job ${jobId} failed: ${err.message}`);
    const resultPath = path.join(jobDriveDir, 'result.json');
    fs.writeFileSync(resultPath, JSON.stringify({
      job_id:      jobId,
      workflow_id: workflowId,
      session_id:  sessionId,
      status:      'error',
      completed_at: new Date().toISOString(),
      error:       err.message,
    }, null, 2));
    updateJobStatus(jobId, 'error', null, err.message);
    flushDbToDrive();
    if (sessionId) {
      pushToStorage(sessionId, {
        session: true,
        profile: false,   // skip profile tar on failure — avoid archiving mid-crash state
        awaitPrimary: false,
        log: msg => log.info(msg),
      }).catch(e => log.warn(`[job-mgr] post-error push failed (non-fatal): ${e.message}`));
    }
    _pushJobToDrive(dirName, false); // push all screenshots for failed job
    if (enableStreaming) emit('job.error', { job_id: jobId, error: err.message });
    // Do NOT re-throw — _tryStartNext's .finally is the safety net; re-throwing here
    // causes an unhandled rejection because executeJob is called concurrently.

  } finally {
    // Release the distributed session lock so other runtimes can use this profile
    if (_sessionLockAcquired && sessionId) {
      releaseSessionLock(sessionId, _nodeName).catch(e =>
        log.warn(`[job-mgr] releaseSessionLock failed (non-fatal): ${e.message}`)
      );
    }
    // Close the page (keep context alive for session reuse)
    if (page) await page.close().catch(() => {});
    // Clear running job reference (pool slot freed by _tryStartNext's .finally)
    _runningJobs.delete(jobId);
    // Evict browser contexts that have been idle for >30 min to prevent Chromium OOM
    evictIdleContexts().catch(() => {});
    // NOTE: we intentionally do NOT clear the exit node here.
    // The system-level exit node is managed by the keep-alive loop, not per-job.
  }
}
