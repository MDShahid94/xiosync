// ─── Patch Ledger ─────────────────────────────────────────────────────────
// Tracks in-memory hot-patches applied via xb_patch.
// Stores: filePath, original content (for rollback), patch timestamp.
// Cleared on server restart. Flushed to git via xb_commit_patch.

import fs from 'node:fs';
import path from 'node:path';
import { createLogger } from '../utils/logger.mjs';

const log = createLogger('patch-ledger');

// In-memory ledger: filePath → { original, patchedAt, description }
const _ledger = new Map();

/**
 * Record a hot-patch. Saves the original content before overwriting.
 * @param {string} filePath  - absolute path to the patched file
 * @param {string} newContent - new file content
 * @param {string} [description] - human description of the change
 */
export function recordPatch(filePath, newContent, description = '') {
  // Save original only on first patch (preserve the true original, not a prev patch)
  if (!_ledger.has(filePath)) {
    let original = null;
    try { original = fs.readFileSync(filePath, 'utf8'); } catch { /* new file */ }
    _ledger.set(filePath, { original, patches: [], filePath });
  }
  const entry = _ledger.get(filePath);
  entry.patches.push({ content: newContent, ts: new Date().toISOString(), description });
  log.info(`[patch-ledger] Recorded patch for ${path.basename(filePath)}`);
}

/** List all patched files with their patch count and latest timestamp. */
export function listPatches() {
  return [..._ledger.entries()].map(([fp, e]) => ({
    file:        fp,
    basename:    path.basename(fp),
    patch_count: e.patches.length,
    patched_at:  e.patches.at(-1)?.ts,
    description: e.patches.at(-1)?.description ?? '',
  }));
}

/** Get the current (latest patch) content of a file. */
export function getPatchedContent(filePath) {
  const e = _ledger.get(filePath);
  if (!e) return null;
  return e.patches.at(-1)?.content ?? null;
}

/** Roll back a file to its original pre-patch content. */
export function rollbackPatch(filePath) {
  const e = _ledger.get(filePath);
  if (!e) return { ok: false, error: 'No patch recorded for this file.' };
  if (e.original === null) {
    // File was new — delete it
    try { fs.unlinkSync(filePath); } catch { /* already gone */ }
  } else {
    fs.writeFileSync(filePath, e.original, 'utf8');
  }
  _ledger.delete(filePath);
  log.info(`[patch-ledger] Rolled back ${path.basename(filePath)}`);
  return { ok: true, rolled_back: filePath };
}

/** Clear all patches from ledger (does NOT restore files). Called after git commit. */
export function clearLedger() {
  _ledger.clear();
  log.info('[patch-ledger] Ledger cleared');
}
