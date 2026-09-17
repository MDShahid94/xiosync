// ─── Action Recorder ──────────────────────────────────────────────────────
// Records browser actions performed via xb_interact / xb_devtools_* tools
// and emits a reusable standalone workflow .mjs file on successful completion.
//
// Sensitive values (passwords, tokens, secrets) are automatically replaced
// with {{param_name}} placeholders and added to the params map — they are
// NEVER written to disk in plaintext.

import fs from 'node:fs';
import path from 'node:path';
import { createLogger } from '../utils/logger.mjs';
import { ensureDir } from '../utils/drive.mjs';

const log = createLogger('recorder');

// Patterns for keys that contain sensitive values — auto-parameterized
const SENSITIVE_KEY_RE = /password|secret|totp|token|auth|key|credential|passphrase/i;

export class ActionRecorder {
  constructor() {
    this._active   = false;
    this._name     = null;
    this._actions  = [];
    this._params   = {};      // paramName → placeholder mapping
    this._startTs  = null;
  }

  get active() { return this._active; }

  start(name = 'recorded_workflow') {
    this._active  = true;
    this._name    = name;
    this._actions = [];
    this._params  = {};
    this._startTs = Date.now();
    log.info(`Recording started: ${name}`);
  }

  stop() {
    this._active = false;
    log.info(`Recording stopped: ${this._name} (${this._actions.length} actions)`);
  }

  /**
   * Record a single tool call.
   * @param {string} tool   - tool name e.g. 'navigate', 'fill', 'click'
   * @param {object} args   - tool arguments (sensitive values auto-parameterized)
   */
  record(tool, args = {}) {
    if (!this._active) return;
    const sanitized = this._sanitize(args);
    this._actions.push({
      t:    Date.now() - this._startTs,
      tool,
      args: sanitized,
    });
  }

  /**
   * Emit a reusable workflow .mjs file and return the output path.
   * Only called on successful job completion.
   * @param {string} [jobId]  - for filename uniqueness
   * @returns {string|null}   - path to emitted file, or null if nothing to emit
   */
  emit(jobId = '') {
    if (!this._actions.length) return null;

    const ts      = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15);
    const slug    = this._name.toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 40);
    const fname   = `${ts}_${slug}.mjs`;
    // Recorded workflows go into the git-managed bundled dir so they are
    // immediately available as plugins without any Drive sync.
    const outDir  = path.join(
      path.dirname(new URL(import.meta.url).pathname), // src/core/
      '..', '..', 'workflows', 'recorded'
    );
    ensureDir(outDir);
    const outPath = path.join(outDir, fname);

    const paramsDoc = Object.keys(this._params).length
      ? JSON.stringify(Object.fromEntries(Object.keys(this._params).map(k => [k, ''])), null, 2)
      : '{}';

    const stepsJs = this._actions.map(a => {
      const argsStr = JSON.stringify(a.args, null, 4)
        .split('\n').map((l, i) => i === 0 ? l : '      ' + l).join('\n');
      return `    // +${a.t}ms\n    { tool: ${JSON.stringify(a.tool)}, args: ${argsStr} },`;
    }).join('\n');

    const content = `// Auto-recorded workflow: ${this._name}
// Generated: ${new Date().toISOString()}
// Job ID: ${jobId}
//
// Usage:
//   xb_run_workflow('recorded/${fname.replace('.mjs','')}', { ${Object.keys(this._params).join(', ')} })
//
// Params (fill in values before running):
// ${paramsDoc}

export async function run(ctx, params = {}) {
  // Validate required params
  const required = ${JSON.stringify(Object.keys(this._params))};
  for (const key of required) {
    if (!params[key]) throw new Error(\`Missing required param: \${key}\`);
  }

  const steps = [
${stepsJs}
  ];

  for (const { tool, args } of steps) {
    // Substitute {{param}} placeholders with actual values
    const resolved = JSON.parse(
      JSON.stringify(args).replace(/\"{{(\\w+)}}\"/g, (_, k) =>
        JSON.stringify(params[k] ?? '')
      )
    );

    // Dispatch to the appropriate page action
    await ctx.step(tool, async () => {
      switch (tool) {
        case 'navigate':
          await ctx.page.goto(resolved.value, { waitUntil: 'domcontentloaded', timeout: 30000 });
          break;
        case 'fill':
          await ctx.page.fill(resolved.selector, resolved.value);
          break;
        case 'click':
          await ctx.page.click(resolved.selector);
          break;
        case 'type':
          await ctx.page.type(resolved.selector, resolved.value, { delay: 80 });
          break;
        case 'press':
          await ctx.page.keyboard.press(resolved.key);
          break;
        case 'waitFor':
          if (resolved.selector)
            await ctx.page.waitForSelector(resolved.selector, { timeout: resolved.timeout ?? 10000 });
          else if (resolved.url)
            await ctx.page.waitForURL(resolved.url, { timeout: resolved.timeout ?? 10000 });
          break;
        case 'evaluate':
          await ctx.page.evaluate(resolved.script);
          break;
        default:
          ctx.log(\`Unknown recorded tool: \${tool} — skipping\`);
      }
    });
  }

  ctx.setResult({ success: true, workflow: ${JSON.stringify(this._name)}, steps: steps.length });
}
`;

    fs.writeFileSync(outPath, content);
    log.info(`Workflow emitted: ${outPath}`);

    // ── Post to XIOSYNC memory graph (non-blocking) ──────────────────────────
    // If XIOSYNC_URL is configured, record this workflow into the memory node
    // graph so it becomes discoverable via /xioflow/memory/search.
    const xiosyncUrl  = process.env.XIOSYNC_URL;
    const xiosyncToken = process.env.XIOSYNC_TOKEN;
    if (xiosyncUrl && xiosyncToken) {
      const memoryPayload = {
        name:             this._name,
        description:      `Auto-recorded workflow: ${this._name}`,
        tier:             'project_experimental',
        status:           'ACTIVE',
        action_type:      'declarative_dag',
        execution_mode:   'sequential',
        volatility_type:  'static',
        recording_method: 'teacher_extension',
        steps: this._actions.map(a => ({
          tool: a.tool,
          args: a.args,
          elapsed_ms: a.t,
        })),
        metadata: { job_id: jobId, emitted_file: fname, params: this._params },
      };
      // Use Node's built-in fetch (Node 18+) or fall back silently
      const fetchFn = typeof fetch !== 'undefined' ? fetch : null;
      if (fetchFn) {
        fetchFn(`${xiosyncUrl}/api/v1/xioflow/memory/record`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${xiosyncToken}`,
          },
          body: JSON.stringify(memoryPayload),
        }).then(r => {
          if (r.ok) log.info(`Memory recorded in XIOSYNC (${this._actions.length} actions)`);
          else log.warn(`XIOSYNC memory record failed: ${r.status}`);
        }).catch(err => log.warn(`XIOSYNC memory record error: ${err.message}`));
      }
    }
    // ─────────────────────────────────────────────────────────────────────────

    this.stop();
    return outPath;
  }

  // ── Private helpers ──────────────────────────────────────────────────────

  _sanitize(args) {
    const result = {};
    for (const [key, val] of Object.entries(args)) {
      if (typeof val === 'string' && SENSITIVE_KEY_RE.test(key)) {
        // Replace sensitive value with {{paramName}} placeholder
        const paramName = this._camelToSnake(key);
        this._params[paramName] = true;
        result[key] = `{{${paramName}}}`;
      } else {
        result[key] = val;
      }
    }
    return result;
  }

  _camelToSnake(str) {
    return str.replace(/([A-Z])/g, '_$1').toLowerCase().replace(/^_/, '');
  }
}

// ── Singleton instance used by mcp-server.mjs ──────────────────────────────
export const recorder = new ActionRecorder();
