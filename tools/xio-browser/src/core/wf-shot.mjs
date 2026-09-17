/**
 * wf-shot.mjs — Generalized screenshot utility for XIO Browser workflows.
 *
 * Usage:
 *   import { createShot } from '../src/core/wf-shot.mjs';
 *
 *   // Inside any workflow step:
 *   const shot = createShot(`${_jobDir}/steps`, { logFn: ctx.log.bind(ctx) });
 *
 *   await shot('01_initial');              // 01_01_initial.jpg (numbered from 1)
 *   await shot('02_login_page');           // 02_02_login_page.jpg
 *   await shot('error', page, { png: true }); // PNG override
 *
 *   // After workflow: copy final shot to job root
 *   if (shot.lastPath) fs.copyFileSync(shot.lastPath, `${_jobDir}/result_final.jpg`);
 */

import { mkdirSync, copyFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Factory that returns a numbered screenshot helper scoped to evDir.
 *
 * @param {string}   evDir     - Directory to write screenshots into (created if absent)
 * @param {object}   [opts]
 * @param {Function} [opts.logFn]     - ctx.log or similar; called with the filename written
 * @param {number}   [opts.quality]   - JPEG quality 1-100 (default 80)
 * @param {number}   [opts.startIdx]  - Starting counter (default 0, first file = 01_)
 * @returns {Function}  shot(label, page?, perShotOpts?) → Promise<string|null>
 *   shot.lastPath  - absolute path of the most recently written screenshot
 *   shot.idx       - current counter value
 */
export function createShot(evDir, opts = {}) {
  const { logFn, quality = 80, startIdx = 0 } = opts;

  // Ensure the directory exists immediately
  try { mkdirSync(evDir, { recursive: true }); } catch {}

  let idx = startIdx;

  /**
   * Take a screenshot and save it to evDir.
   *
   * @param {string}           label         - Human-readable label (used in filename)
   * @param {Page|null}        [page]        - Playwright page object; if omitted uses last page set via shot.setPage()
   * @param {object}           [perShotOpts]
   * @param {boolean}          [perShotOpts.png]      - Save as PNG instead of JPEG
   * @param {number}           [perShotOpts.quality]  - Override JPEG quality
   * @param {boolean}          [perShotOpts.fullPage] - Capture full scrolled page (default false)
   * @param {number}           [perShotOpts.waitMs]   - Wait this many ms before snapping
   * @returns {Promise<string|null>} Absolute path written, or null on error
   */
  async function shot(label, page, perShotOpts = {}) {
    // Allow shot(label) or shot(label, opts) when page not passed
    if (page && typeof page === 'object' && !page.screenshot) {
      perShotOpts = page;
      page = null;
    }
    const activePage = page ?? shot._page;
    if (!activePage) {
      logFn?.(`[wf-shot] ⚠️  No page for screenshot "${label}" — skipped`);
      return null;
    }

    const { png = false, quality: q = quality, fullPage = false, waitMs = 0 } = perShotOpts;
    const ext = png ? 'png' : 'jpg';
    const num = String(++idx).padStart(2, '0');
    const filename = `${num}_${label}.${ext}`;
    const fullPath = join(evDir, filename);

    if (waitMs > 0) await new Promise(r => setTimeout(r, waitMs));

    try {
      await activePage.screenshot({
        path: fullPath,
        type: png ? 'png' : 'jpeg',
        quality: png ? undefined : q,
        fullPage,
        animations: 'disabled', // prevents font-loading stall on cold browser pages
        timeout: 15_000,
      });
      shot.lastPath = fullPath;
      logFn?.(`📸 ${filename}`);
      return fullPath;
    } catch (e) {
      logFn?.(`[wf-shot] ⚠️  Screenshot "${label}" failed: ${e.message}`);
      return null;
    }
  }

  /** Set a default page so callers don't need to pass it every time */
  shot.setPage = (page) => { shot._page = page; };

  /** Current idx (number of shots taken so far) */
  shot.idx = 0;
  Object.defineProperty(shot, 'idx', { get: () => idx });

  /** Absolute path of the most recently written screenshot (null until first shot) */
  shot.lastPath = null;

  /** Internal default page */
  shot._page = null;

  return shot;
}
