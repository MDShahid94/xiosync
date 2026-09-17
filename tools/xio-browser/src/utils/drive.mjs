// ─── Drive Path Helpers ────────────────────────────────────────────────────
// All paths are resolved relative to the Drive root set at startup.
// The Drive root is copied to local SSD on boot; at runtime we work locally.

import path from 'node:path';
import { existsSync, mkdirSync } from 'node:fs';

let _driveRoot = '/content/drive/Shareddrives/XIO_MESH';

export function setDriveRoot(root) {
  _driveRoot = root;
  ensureDir(root);
}

export function driveRoot() { return _driveRoot; }

// Paths within the Drive root — all entries MUST be DIRECTORIES.
// Do NOT add file paths here: ensureAllDirs() calls mkdirSync on every zero-arg entry.
export const Paths = {
  tailscaleStates: () => path.join(_driveRoot, 'tailscale_states'),
  sessions:        () => path.join(_driveRoot, 'sessions'),
  chromeProfiles:  () => path.join(_driveRoot, 'chrome_profiles'),
  jobs:            () => path.join(_driveRoot, 'jobs'),
  jobDir:    (id)  => path.join(_driveRoot, 'jobs', id),
};

// Separate export for the DB FILE path — intentionally NOT in Paths so
// ensureAllDirs() never mistakes it for a directory.
export const dbFilePath = () => path.join(_driveRoot, 'xio-browser.db');

export function ensureDir(p) {
  if (!existsSync(p)) mkdirSync(p, { recursive: true });
}

export function ensureAllDirs() {
  Object.values(Paths).forEach(fn => {
    try { if (typeof fn === 'function' && fn.length === 0) ensureDir(fn()); }
    catch { /* skip parameterized paths */ }
  });
}
