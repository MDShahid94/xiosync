/**
 * sub-workflow.mjs — General Sub-Workflow Context Factory
 *
 * Creates a scoped sub-context within a parent job that:
 *   - Stores all logs and screenshots in <job_dir>/<name>_{DATE}_{TIME}_{6hex}/steps/
 *   - Prefixes all log messages with [<name>]
 *   - Exposes step(), screenshot(), and log() helpers
 *   - Writes a sub_result.json on completion (pass/fail + step log)
 *
 * Sub-job directory hierarchy (no intermediate sub-jobs/ dir):
 *   self-spawn_20260809_071750_60a641/    (parent job)
 *     checkpoint.json
 *     steps/                              ← parent screenshots
 *     tailscale-auth_20260809_072057_ab1234/  ← sub-workflow dir (directly inside)
 *       steps/                            ← sub-workflow screenshots
 *       sub_result.json
 *
 * Usage:
 *   const tsCtx = createSubWorkflowContext(ctx, 'tailscale-auth');
 *   await tsCtx.step('open_url', async () => {
 *     await tsCtx.screenshot(page, 'step1_opened');
 *     tsCtx.log('URL opened');
 *   });
 *   const result = tsCtx.getResult();
 */

import fs from 'node:fs';
import path from 'node:path';

/**
 * @param {object} parentCtx - The parent workflow context (must have .log and .dirName)
 * @param {string} name      - Sub-workflow name (used as subdirectory name and log prefix)
 * @returns {SubWorkflowContext}
 */
export function createSubWorkflowContext(parentCtx, name) {
  // Resolve the sub-workflow directory inside the main job dir
  const jobDir = parentCtx.dirName
    ? `/content/xio-mesh/jobs/${parentCtx.dirName}`
    : '/tmp/xio-jobs-unknown';

  // Sub-job dir: {name}_{YYYYMMDD}_{HHMMSS}_{6hex}/ directly inside parent job dir.
  // Same format as top-level jobs. No intermediate "sub-jobs/" directory.
  const _swNow  = new Date();
  const _dt     = _swNow.toISOString().slice(0, 10).replace(/-/g, '') + '_' + _swNow.toISOString().slice(11, 19).replace(/:/g, '');
  const _sid    = Date.now().toString(36).slice(-6);                    // unique 6-char base36 suffix
  const slug    = `${name.replace(/[^a-zA-Z0-9-]/g,'-').replace(/^-+|-+$/g,'')}_${_dt}_${_sid}`;
  const subDir  = path.join(jobDir, slug);  // directly inside parent (no sub-jobs/)
  const stepsDir = path.join(subDir, 'steps');
  fs.mkdirSync(stepsDir, { recursive: true });

  const steps   = [];
  const prefix  = `[sub:${name}]`;
  let   stepIdx = 0;

  /** Log to both the sub-context (JSON) and the parent ctx */
  function log(msg) {
    const ts  = new Date().toISOString();
    const full = `${prefix} ${msg}`;
    parentCtx.log?.(full);
    const last = steps.at(-1);
    if (last && last.status === 'running') last.logs.push(`${ts} ${msg}`);
  }

  /** Take a screenshot and save it inside the sub-workflow steps/ dir */
  async function screenshot(page, label) {
    try {
      const safe = label.replace(/[^a-zA-Z0-9_-]/g, '_');
      const file = path.join(stepsDir, `${String(stepIdx).padStart(2,'0')}_${safe}.png`);
      await page.screenshot({ path: file, fullPage: false });
      log(`📸 ${path.basename(file)}`);
      return file;
    } catch (e) {
      log(`⚠️  screenshot failed: ${e.message}`);
      return null;
    }
  }

  /**
   * Run a named sub-step. Errors are caught and recorded; the sub-workflow
   * continues to the next step unless throwOnFail=true.
   */
  async function step(stepName, fn, { throwOnFail = false } = {}) {
    stepIdx++;
    const record = { name: stepName, status: 'running', logs: [], startedAt: Date.now() };
    steps.push(record);
    log(`▶ step: ${stepName}`);
    try {
      await fn();
      record.status = 'done';
      record.finishedAt = Date.now();
      log(`✅ step done: ${stepName}`);
    } catch (e) {
      record.status = 'error';
      record.error  = e.message;
      record.finishedAt = Date.now();
      log(`❌ step failed: ${stepName}: ${e.message}`);
      if (throwOnFail) throw e;
    }
    _writeResult();
  }

  function _writeResult() {
    try {
      const ok = steps.every(s => s.status !== 'error');
      fs.writeFileSync(
        path.join(subDir, 'sub_result.json'),
        JSON.stringify({ name, ok, steps }, null, 2)
      );
    } catch {}
  }

  function getResult() {
    return { name, ok: steps.every(s => s.status !== 'error'), steps };
  }

  return { log, screenshot, step, getResult, dir: subDir, stepsDir, name };
}
