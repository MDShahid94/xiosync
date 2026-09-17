// ─── Workflow Runner ───────────────────────────────────────────────────────
// Hot-loads .mjs workflow plugins exclusively from:
//   xio-browser/workflows/  ← GIT-MANAGED, SINGLE SOURCE OF TRUTH
//
// xio-mesh/workflows/ on Drive is no longer used — it has been removed.
// Workflows are versioned in git. To update a running node without restart:
//   xb_patch rel_path="workflows/foo.mjs" → writes directly to the bundled dir
//   xb_commit_patch → git commit + push
// For a full sync: xb_restart pull_code:true
//
// xb_workflow_push hot-patches the local bundled dir in-place (no Drive I/O).

import path from 'node:path';
import fs from 'node:fs';
import { createLogger } from '../utils/logger.mjs';

const log = createLogger('workflow-runner');

// Bundled (git) workflows dir — always the canonical location
const BUNDLED_DIR = new URL('../../workflows', import.meta.url).pathname;

// ── Workflow Discovery ─────────────────────────────────────────────────────

export function listWorkflows() {
  const candidates = [];
  if (fs.existsSync(BUNDLED_DIR)) {
    fs.readdirSync(BUNDLED_DIR)
      .filter(f => f.endsWith('.mjs'))
      .forEach(f => {
        const id = path.basename(f, '.mjs');
        candidates.push({ id, path: path.join(BUNDLED_DIR, f), source: 'bundled' });
      });
  }
  return candidates;
}

export function getWorkflowSource(id) {
  const p = path.join(BUNDLED_DIR, `${id}.mjs`);
  if (!fs.existsSync(p)) return null;
  return fs.readFileSync(p, 'utf8');
}

export function writeWorkflow(id, source) {
  return patchWorkflowFile(id, source);
}

/**
 * Hot-patch: write a workflow file directly to the bundled dir WITHOUT git.
 * The next call to runWorkflow() will pick it up via the mtime cache-bust.
 * Used by xb_patch. Record in patch-ledger for later git commit.
 * @param {string} id      - workflow ID (no .mjs extension)
 * @param {string} source  - full .mjs source
 * @returns {{ ok: boolean, file: string, previous_existed: boolean }}
 */
export function patchWorkflowFile(id, source) {
  const filePath = path.join(BUNDLED_DIR, `${id}.mjs`);
  const existed  = fs.existsSync(filePath);
  fs.writeFileSync(filePath, source, 'utf8');
  log.info(`[workflow-runner] Hot-patched workflow: ${id}`);
  return { ok: true, file: filePath, previous_existed: existed };
}

/**
 * Hot-patch any arbitrary file in the xio-browser repository.
 * Validates the path is within /content/xio-browser for safety.
 * @param {string} relPath  - path relative to /content/xio-browser (e.g. "src/core/job-manager.mjs")
 * @param {string} content  - new file content
 * @returns {{ ok: boolean, abs_path: string }}
 */
export function patchArbitraryFile(relPath, content) {
  const REPO_ROOT  = '/content/xio-browser';
  // Security: resolve and ensure path stays within repo
  const absPath = path.resolve(REPO_ROOT, relPath);
  if (!absPath.startsWith(REPO_ROOT + path.sep) && absPath !== REPO_ROOT) {
    throw new Error(`Path traversal rejected: ${relPath} resolves outside repo root.`);
  }
  fs.mkdirSync(path.dirname(absPath), { recursive: true });
  fs.writeFileSync(absPath, content, 'utf8');
  log.info(`[workflow-runner] Hot-patched arbitrary file: ${relPath}`);
  return { ok: true, abs_path: absPath };
}

export function deleteWorkflow(id) {
  return { ok: false, error: 'Workflows are managed via git and synced on restart. Remove the workflow from the repository instead of using xb_workflow_delete.' };
}

export function getWorkflowMeta(id) {
  // Dynamic import meta without running the full module
  // Reads the `export const meta = {...}` block and parses it safely
  const src = getWorkflowSource(id);
  if (!src) return null;
  try {
    // Extract the meta block — only accept pure JSON object literals (no code)
    const metaMatch = src.match(/export\s+const\s+meta\s*=\s*(\{[\s\S]+?\});/);
    if (!metaMatch) return { id };
    // Replace JS property syntax with JSON: wrap keys in quotes, replace trailing commas
    const jsonStr = metaMatch[1]
      .replace(/\/\/[^\n]*/g, '')          // strip line comments
      .replace(/,\s*([}\]])/g, '$1')        // trailing commas
      .replace(/([{,]\s*)(\w+)\s*:/g, '$1"$2":')  // unquoted keys → quoted
      .replace(/'/g, '"');                  // single → double quotes
    return { id, ...JSON.parse(jsonStr) };
  } catch {
    return { id };
  }
}

// ── Workflow Execution ─────────────────────────────────────────────────────

/**
 * Load and run a workflow plugin.
 *
 * @param {string}   workflowId  - filename without .mjs
 * @param {object}   ctx         - the ctx object passed to run(ctx, params)
 * @param {object}   params      - workflow-specific params
 */
export async function runWorkflow(workflowId, ctx, params) {
  const filePath = path.join(BUNDLED_DIR, `${workflowId}.mjs`);
  
  if (!fs.existsSync(filePath)) {
    throw new Error(`Workflow not found: ${workflowId} (checked bundled dir only)`);
  }

  // Cache-bust: append mtime to force re-import on every run
  const mtime = fs.statSync(filePath).mtimeMs;
  const moduleUrl = `file://${filePath}?v=${mtime}`;

  log.info(`Loading workflow: ${workflowId} (v=${mtime})`);
  const mod = await import(moduleUrl);

  if (typeof mod.run !== 'function') {
    throw new Error(`Workflow ${workflowId} does not export a run() function`);
  }

  await mod.run(ctx, params);
}
