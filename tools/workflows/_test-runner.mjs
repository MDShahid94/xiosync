/**
 * XIO Mesh — Workflow Test Runner
 * ================================
 * Composable testing layer for browser automation workflows.
 *
 * Injects into `ctx`:
 *   ctx.verify(selector, opts)      — assert a DOM state, retry & screenshot on fail
 *   ctx.screenshot(label)           — capture evidence screenshot
 *   ctx.testStep(name, fn, verify)  — step wrapper with pre/post evidence + auto-retry
 *   ctx.hitl(message)               — pause job for agent review; resumes via xb_job_resume
 *   ctx.pause(reason)               — explicit pause checkpoint (set by job-manager)
 *
 * HITL Decision Policy (agent-driven):
 *   1. Attempt autonomously up to MAX_RETRIES times with progressive delays.
 *   2. Before each retry, take a screenshot and inspect DOM for a recovery path.
 *   3. Only escalate to HITL when no known recovery is possible (unknown state).
 *
 * Auto-push to Drive:
 *   On full test pass, the workflow file is pushed to Drive via sync.py.
 *   Drive workflows are the "verified canonical" versions.
 *
 * Usage (inside a workflow):
 *   import { attachTestRunner } from './_test-runner.mjs';
 *   export async function run(ctx, params) {
 *     attachTestRunner(ctx, import.meta.url);
 *     await ctx.testStep('my_step', async () => { ... }, {
 *       verify: 'selector-that-should-exist',
 *       label:  'human readable verification description',
 *     });
 *   }
 */

import path from 'path';
import fs   from 'fs';
import { fileURLToPath } from 'url';
import { Paths } from '../src/utils/drive.mjs';

const MAX_RETRIES     = 3;
const RETRY_DELAY_MS  = 2500;
const VERIFY_TIMEOUT  = 10_000;

/**
 * Attach the test runner to the workflow context.
 * @param {object} ctx        - workflow context (has ctx.page, ctx.log, ctx.step, etc.)
 * @param {string} workflowUrl - import.meta.url of the calling workflow
 */
export function attachTestRunner(ctx, workflowUrl) {
  const workflowFile = workflowUrl ? fileURLToPath(workflowUrl) : null;
  const workflowId   = workflowFile ? path.basename(workflowFile, '.mjs') : 'unknown';
  const report       = { workflow: workflowId, steps: [], passed: true, ts: new Date().toISOString() };

  // ── Evidence directory ──────────────────────────────────────────────────
  // Store screenshots alongside the job output.
  // ctx.dirName is the datetime-prefixed dir name (YYYYMMDD_HHMMSS_jobId);
  // Paths.jobs() gives the absolute base: /content/xio-mesh/jobs
  function evidenceDir() {
    let base;
    try {
      // ctx.jobDir = full absolute path (correct for nested sub-workflow dirs)
      // ctx.dirName = leaf name only — use as fallback if jobDir not set
      base = ctx.jobDir
        ?? (ctx.dirName ? path.join(Paths.jobs(), ctx.dirName) : null)
        ?? '/tmp/xio_test_evidence';
    } catch {
      base = '/tmp/xio_test_evidence';
    }
    const dir = path.join(base, 'steps');  // was 'test_evidence'
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  // ── Screenshot capture ─────────────────────────────────────────────────
  ctx.screenshot = async function(label = 'screenshot') {
    if (!ctx.page) return null;
    try {
      const fname = `${Date.now()}_${label.replace(/[^a-z0-9_-]/gi, '_')}.png`;
      const fpath = path.join(evidenceDir(), fname);
      await ctx.page.screenshot({ path: fpath, fullPage: true });
      ctx.log(`📸 Screenshot: ${fname}`);
      return fpath;
    } catch (e) {
      ctx.log(`⚠️  Screenshot failed: ${e.message}`);
      return null;
    }
  };

  // ── DOM verify ─────────────────────────────────────────────────────────
  /**
   * Assert a DOM selector exists (or a custom predicate returns true).
   * @param {string|Function} selectorOrFn  - CSS selector string OR async fn(page)→bool
   * @param {object} opts
   *   opts.label      - human description (for logging)
   *   opts.timeout    - ms to wait (default VERIFY_TIMEOUT)
   *   opts.negate     - if true, assert selector is ABSENT
   */
  ctx.verify = async function(selectorOrFn, opts = {}) {
    const label   = opts.label   ?? (typeof selectorOrFn === 'string' ? selectorOrFn : 'custom check');
    const timeout = opts.timeout ?? VERIFY_TIMEOUT;
    const negate  = opts.negate  ?? false;

    try {
      if (typeof selectorOrFn === 'function') {
        const ok = await Promise.race([
          selectorOrFn(ctx.page).then(v => !!v),
          new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), timeout)),
        ]);
        if (negate ? ok : !ok) throw new Error(`Custom verify failed: ${label}`);
      } else {
        if (negate) {
          // assert it's gone
          const el = await ctx.page.$(selectorOrFn);
          if (el) throw new Error(`Expected absent but found: ${selectorOrFn}`);
        } else {
          await ctx.page.waitForSelector(selectorOrFn, { timeout, state: 'visible' });
        }
      }
      ctx.log(`✅ Verified: ${label}`);
      return true;
    } catch (e) {
      ctx.log(`❌ Verify failed: ${label} — ${e.message}`);
      return false;
    }
  };

  // ── HITL escalation ────────────────────────────────────────────────────
  /**
   * Escalate to agent review.
   *
   * Behaviour:
   *   1. Take a HITL screenshot and write hitl_notice.json to evidence dir.
   *   2. If a pause was requested for this job (via xb_job_pause), call ctx.pause()
   *      which suspends execution until the agent calls xb_job_resume.
   *      The agent can then fix the issue with xb_patch and resume seamlessly.
   *   3. Otherwise throw an error so the job surfaces as "error" requiring review.
   *
   * Agent workflow:
   *   a. Receive job.hitl SSE event (or poll xb_job_list for paused status).
   *   b. Read evidence via job files_url.
   *   c. Fix with xb_patch.
   *   d. Call xb_job_resume to continue.
   */
  ctx.hitl = async function(message, opts = {}) {
    ctx.log(`🛑 HITL REQUIRED: ${message}`);
    const shotPath = await ctx.screenshot('HITL');  // ctx.screenshot() already prepends timestamp

    const evDir  = evidenceDir();
    const notice = path.join(evDir, 'hitl_notice.json');
    const hitlPayload = {
      message,
      ts:              new Date().toISOString(),
      job_id:          ctx.jobId ?? null,
      shot_path:       shotPath,
      instructions:    opts.instructions ?? null,
      action_required: 'Complete the step on your device, then inform the agent. Agent calls xb_job_resume to continue.',
      tools: ['xb_job_resume', 'xb_devtools_screenshot'],
    };
    fs.writeFileSync(notice, JSON.stringify(hitlPayload, null, 2));

    // Emit SSE event for real-time agent notification
    try {
      globalThis.__xioEmitEvent?.('job.hitl', {
        job_id:       ctx.jobId,
        message,
        shot_path:    shotPath,
        instructions: opts.instructions ?? null,
      });
    } catch {}

    // Mark HITL as pending — workflow is responsible for calling ctx.setResult() after resume
    ctx._hitlPending = { hitl: true, message, ts: new Date().toISOString() };

    // ALWAYS auto-pause — do not require prior __xioPauseRequested flag
    if (typeof ctx.pause === 'function') {
      // Clean up any stale pause request flag
      globalThis.__xioPauseRequested?.delete(ctx.jobId);
      await ctx.pause(`HITL: ${message}`);
      ctx.log('✅ Resumed after HITL pause — continuing workflow.');
      return;  // do NOT throw — let the workflow continue
    }

    // Fallback: no pause support — throw so job surfaces as error
    throw new Error(`[HITL] ${message}`);
  };

  // ── Smart step wrapper ─────────────────────────────────────────────────
  /**
   * Run a step with evidence collection and auto-retry.
   *
   * @param {string}   name     - step identifier (snake_case)
   * @param {Function} fn       - async action to perform
   * @param {object}   opts
   *   opts.verify          - CSS selector or async fn(page)→bool to check after action
   *   opts.verifyLabel     - human label for the verify check
   *   opts.recoveryFn      - async fn(ctx) called before each retry to attempt recovery
   *   opts.hitlMessage     - custom message if all retries fail (triggers HITL)
   *   opts.autoResolvable  - boolean hint; false → HITL immediately on first fail
   *   opts.screenshotAfter - take screenshot after success (default true)
   */
  ctx.testStep = async function(name, fn, opts = {}) {
    const {
      verify          = null,
      verifyLabel     = name,
      verifyOpts      = {},
      recoveryFn      = null,
      hitlMessage     = null,
      autoResolvable  = true,
      screenshotAfter = true,
    } = opts;

    const stepRecord = { name, attempts: 0, passed: false, screenshots: [] };
    let lastError    = null;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      stepRecord.attempts = attempt;
      try {
        ctx.log(`[test] Step: ${name} (attempt ${attempt}/${MAX_RETRIES})`);

        // Run the action
        await fn();

        // Verify the outcome if a verifier is provided
        if (verify) {
          const ok = await ctx.verify(verify, { label: verifyLabel, ...verifyOpts });
          if (!ok) throw new Error(`Post-step verification failed: ${verifyLabel}`);
        }

        // Success — capture evidence screenshot
        if (screenshotAfter) {
          const shot = await ctx.screenshot(`${name}_ok_attempt${attempt}`);
          if (shot) stepRecord.screenshots.push(shot);
        }

        stepRecord.passed = true;
        ctx.log(`✅ Step "${name}" passed`);
        break;

      } catch (e) {
        lastError = e;
        ctx.log(`⚠️  Step "${name}" attempt ${attempt} failed: ${e.message}`);
        const shot = await ctx.screenshot(`${name}_fail_attempt${attempt}`);
        if (shot) stepRecord.screenshots.push(shot);

        if (!autoResolvable) {
          // Agent decided this cannot be auto-resolved → HITL immediately
          await ctx.hitl(hitlMessage ?? `Step "${name}" is not auto-resolvable: ${e.message}`);
          return; // never reached (hitl throws)
        }

        if (attempt < MAX_RETRIES) {
          // Recovery attempt before retry
          if (recoveryFn) {
            try {
              ctx.log(`[test] Running recovery for "${name}"…`);
              await recoveryFn(ctx);
            } catch (re) {
              ctx.log(`[test] Recovery also failed: ${re.message}`);
            }
          }
          await ctx.page?.waitForTimeout(RETRY_DELAY_MS);
        }
      }
    }

    report.steps.push(stepRecord);

    if (!stepRecord.passed) {
      report.passed = false;
      await ctx.hitl(
        hitlMessage ??
        `Step "${name}" failed after ${MAX_RETRIES} attempts. Last error: ${lastError?.message}`
      );
    }
  };

  // ── Finalise test report ───────────────────────────────────────────────
  // Called automatically by wrapping ctx.setResult
  const _originalSetResult = ctx.setResult?.bind(ctx);
  ctx.setResult = function(result) {
    // Write test report to evidence dir
    try {
      report.finished = new Date().toISOString();
      report.result   = result;
      fs.writeFileSync(
        path.join(evidenceDir(), 'test_report.json'),
        JSON.stringify(report, null, 2)
      );
      const passedCount = report.steps.filter(s => s.passed).length;
      ctx.log(`📋 Test report: ${passedCount}/${report.steps.length} steps passed`);
    } catch (e) {
      ctx.log(`⚠️  Could not write test report: ${e.message}`);
    }

    if (_originalSetResult) _originalSetResult(result);
  };

  ctx.log(`🧪 Test runner attached to workflow: ${workflowId}`);
  return report;
}
